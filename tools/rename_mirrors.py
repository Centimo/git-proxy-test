#!/usr/bin/env python3
"""One-off migration: rename existing Forgejo mirrors to the current naming scheme.

Mirrors created before the multi-host rework are named `<owner>__<repo>`. The current
scheme is `SourceRepo.mirror_name()` — a slug plus an 8-hex hash of host+path. Deploying
the new proxy without renaming would leave every existing mirror unreachable by name, so
each one would be re-migrated from scratch (gigabytes over the link). Renaming through the
Forgejo API moves the on-disk repository too, so no data is refetched.

The new name is derived from each repo's `original_url` — recorded by Forgejo at migration
time and inverted by `SourceRegistry.parse_original_url()` — never guessed from the old name.

Dry run by default; pass --apply to perform the renames. A repo whose `original_url` cannot be
mapped at all — a traversal segment or a name the proxy rejects — stops the run, since a partial
rename would leave the cache half-migrated; --ignore-unresolved migrates the rest and leaves those
entries under their old names.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

import source

DEFAULT_TOKEN_FILE = "/workspace/cache/git-proxy/gitea/proxy-token"
DEFAULT_FORGEJO_URL = "http://127.0.0.1:3000"
DEFAULT_USER = "gitadmin"
PAGE_SIZE = 50


def api(url: str, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
  req = urllib.request.Request(
    f"{url}/api/v1{path}",
    data=json.dumps(body).encode() if body is not None else None,
    method=method,
    headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
  )
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:
      raw = resp.read()
      return resp.status, (json.loads(raw) if raw else {})
  except urllib.error.HTTPError as e:
    try:
      return e.code, json.loads(e.read())
    except Exception:
      return e.code, {}


def list_repos(url: str, token: str, user: str) -> list[dict]:
  repos: list[dict] = []
  page = 1
  while True:
    status, data = api(url, token, "GET", f"/users/{user}/repos?page={page}&limit={PAGE_SIZE}")
    if status != 200:
      sys.exit(f"FATAL: listing repos failed: status={status} body={str(data)[:200]}")
    if not data:
      return repos
    repos.extend(data)
    if len(data) < PAGE_SIZE:
      return repos
    page += 1


def unresolved_reason(url) -> str:
  """Why parse_original_url rejected this original_url, in words. Mirrors its checks; used
  only for the report, so the decision itself still comes from parse_original_url."""
  if not isinstance(url, str) or not url:
    return "no original_url recorded"
  for host in source.REGISTRY.hosts():
    prefix = source.REGISTRY.base_url(host) + "/"
    if not url.startswith(prefix):
      continue
    path = url[len(prefix):].strip("/").removesuffix(".git")
    if not path:
      return "no repository path after the host"
    for seg in path.split("/"):
      if seg in (".", ".."):
        return f"path segment {seg!r} — a traversal artifact; no such repository upstream"
    return "a path segment is not a valid repository name"
  return f"host outside PROXY_SOURCES ({', '.join(source.REGISTRY.hosts())})"


def build_plan(repos: list[dict]) -> tuple[list[tuple[str, str]], list[str], list[str]]:
  """Returns (renames, already_current, unresolved): renames as (old_name, new_name)."""
  renames: list[tuple[str, str]] = []
  already_current: list[str] = []
  unresolved: list[str] = []
  for repo in repos:
    name = repo["name"]
    src = source.REGISTRY.parse_original_url(repo.get("original_url"))
    if src is None:
      unresolved.append(f"{name}: {unresolved_reason(repo.get('original_url'))} "
                        f"(original_url={repo.get('original_url')!r})")
      continue
    new_name = src.mirror_name()
    if new_name == name:
      already_current.append(name)
    else:
      renames.append((name, new_name))
  return renames, already_current, unresolved


def validate(renames: list[tuple[str, str]], all_names: set[str]):
  """Aborts on anything that would make the rename ambiguous or destructive."""
  new_names = [new for _, new in renames]
  duplicates = {n for n in new_names if new_names.count(n) > 1}
  if duplicates:
    sys.exit(f"FATAL: several repos map to the same new name: {sorted(duplicates)}")

  renamed_from = {old for old, _ in renames}
  # A new name that belongs to a repo which is not itself being renamed away would collide.
  occupied = {n for n in new_names if n in all_names and n not in renamed_from}
  if occupied:
    sys.exit(f"FATAL: new names already taken by other repos: {sorted(occupied)}")

  # A new name equal to another repo's old name needs ordering care; the hash suffix makes
  # this impossible in practice, so treat it as a stop rather than implementing two-phase renames.
  overlap = {n for n in new_names if n in renamed_from}
  if overlap:
    sys.exit(f"FATAL: new names collide with old names still in use: {sorted(overlap)}")


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--apply", action="store_true", help="perform the renames (default: dry run)")
  parser.add_argument("--ignore-unresolved", action="store_true",
                      help="migrate the mappable repos and leave the unmappable ones under their old names")
  parser.add_argument("--forgejo-url", default=os.environ.get("FORGEJO_URL", DEFAULT_FORGEJO_URL))
  parser.add_argument("--user", default=os.environ.get("FORGEJO_USER", DEFAULT_USER))
  parser.add_argument("--token-file", default=os.environ.get("FORGEJO_TOKEN_FILE", DEFAULT_TOKEN_FILE))
  args = parser.parse_args()

  token = os.environ.get("FORGEJO_TOKEN", "")
  if not token:
    try:
      with open(args.token_file) as f:
        token = f.read().strip()
    except OSError as e:
      sys.exit(f"FATAL: cannot read token from {args.token_file}: {e}")
  if not token:
    sys.exit("FATAL: empty Forgejo token")

  print(f"sources allowlist : {sorted(source.REGISTRY.hosts())}")
  repos = list_repos(args.forgejo_url, token, args.user)
  print(f"repos found       : {len(repos)}")

  renames, already_current, unresolved = build_plan(repos)
  if unresolved:
    print(f"\nunresolved ({len(unresolved)}):")
    for u in unresolved:
      print(f"  {u}")
    if not args.ignore_unresolved:
      sys.exit("FATAL: some repos could not be mapped to the current scheme; renaming only part of them\n"
               "would leave the cache half-migrated. An entry rejected for a traversal segment or an\n"
               "invalid name can never be reached through the proxy under any scheme — pass\n"
               "--ignore-unresolved to migrate the rest and leave those under their old names.")
    print("\n  --ignore-unresolved: kept under their old names, unreachable through the proxy")

  validate(renames, {r["name"] for r in repos})

  print(f"already current   : {len(already_current)}")
  print(f"to rename         : {len(renames)}\n")
  for old, new in renames:
    print(f"  {old}  ->  {new}")

  if not args.apply:
    print("\nDRY RUN — nothing changed. Re-run with --apply to perform these renames.")
    return

  print()
  failed = []
  stale = []
  for i, (old, new) in enumerate(renames, 1):
    status, data = api(args.forgejo_url, token, "PATCH", f"/repos/{args.user}/{urllib.parse.quote(old)}", {"name": new})
    message = data.get("message", "") if isinstance(data, dict) else ""
    if status == 200:
      print(f"  [{i}/{len(renames)}] {old} -> {new}")
    elif status == 422 and "no such file or directory" in message:
      # Forgejo has the repository row but its directory is gone — nothing to rename and
      # nothing to lose; the proxy re-creates such a mirror on demand once the entry is dropped.
      stale.append(old)
      print(f"  [{i}/{len(renames)}] SKIPPED {old}: no repository on disk (stale Forgejo entry)")
    else:
      failed.append((old, new, status, str(data)[:200]))
      print(f"  [{i}/{len(renames)}] FAILED {old} -> {new}: status={status} body={str(data)[:200]}")

  print(f"\nrenamed: {len(renames) - len(failed) - len(stale)}/{len(renames)}")
  if stale:
    print(f"stale entries with no repository on disk ({len(stale)}): {', '.join(stale)}")
    print("  delete them in Forgejo — the proxy migrates them again on the next request")
  if failed:
    sys.exit(f"FATAL: {len(failed)} renames failed; the cache is now partly migrated — "
             "re-run this script to retry the remaining ones")


if __name__ == "__main__":
  main()
