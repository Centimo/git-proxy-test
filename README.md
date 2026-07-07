# git-proxy

HTTP-прокси перед Forgejo, который **по требованию** зеркалирует GitHub-репозитории и
на каждый `git clone`/`fetch` гарантирует их свежесть. Плюс webhook-механизм, который на
пуш релизного тега в зеркало автоматически заводит ветку с обновлённым `packages.yml`
в conan-common.

## Зачем

Ускорить и сделать надёжнее CI-сборки, которым нужно клонировать репозитории с GitHub.
В частности — для корректного RREV в `conan graph build-order` + lockfile-подходе, где
клонируются репозитории с `export_sources()` (protobuf, Simd, onnxruntime и т.п.).

CI обращается к прокси вместо GitHub. Прокси держит локальные зеркала в Forgejo,
поэтому клоны идут из локальной сети, а не с github.com.

## Как это работает

Клиент подменяет github.com на адрес прокси:

```bash
git config --global url."http://<proxy-host>:8080/".insteadOf "https://github.com/"
```

После этого `git clone https://github.com/owner/repo` фактически идёт на
`http://<proxy-host>:8080/owner/repo`, и прокси обрабатывает запрос так:

| Состояние зеркала в Forgejo         | Действие прокси                                              |
|-------------------------------------|-------------------------------------------------------------|
| Не существует                       | Создаёт mirror (Forgejo API `POST /repos/migrate`), возвращает git-`ERR` → клиент делает retry |
| Существует, но пустое (`empty:true`) | Дёргает `mirror-sync`, ждёт заполнения; пока не готово — git-`ERR` (retry) |
| Существует и заполнено              | Проксирует запрос в Forgejo; клиент получает данные          |

### Проверка свежести (freshness)

Ключевая особенность — на каждый `GET /owner/repo/info/refs?service=git-upload-pack`
(первый запрос любого `clone`/`fetch`) прокси сравнивает refs зеркала с upstream и при
расхождении форсирует синхронизацию **синхронно**, до того как отдать данные клиенту.
Реализовано в классе `MirrorFreshness` (`proxy/proxy.py`):

1. `git ls-remote` на GitHub (`refs/heads/*` + `refs/tags/*`), результат кешируется на
   `LS_REMOTE_CACHE_TTL=30 с`; одновременные запросы одного репо коалесцируются (single-flight).
2. `git ls-remote` на зеркало в Forgejo.
3. Если наборы `(refname → sha)` совпадают — отдаём как есть.
4. Если различаются — `POST /mirror-sync` и ждём до `REF_SYNC_WAIT_TIMEOUT=120 с`, пока
   refs сойдутся. Дедупликация: параллельный второй sync того же репо не запускается.
5. **Fail-open:** при любой ошибке обращения к upstream прокси всё равно проксирует запрос
   (лучше отдать чуть устаревшее зеркало, чем упасть).

> Эта проверка появилась в коммите `af2ef85` («proxy: ensure mirror freshness on info/refs
> requests»). На старом образе её нет — там свежесть обновляется только по расписанию Forgejo
> и вручную через `mirror-sync`.

### Схема имён зеркал

Зеркало в Forgejo называется `owner__repo` (двойное подчёркивание), владелец — админ Forgejo
(`gitadmin` на проде). Пример: `github.com/Centimo/onnxruntime` → `gitadmin/Centimo__onnxruntime`.

> Ограничение: если `owner` или `repo` сами содержат `__`, обратный разбор имени
> (`hooks.py:_parse_mirror_full_name`) неоднозначен. Известное ограничение схемы-делимитера.

## Webhook → conan-common

Отдельная функция (`proxy/hooks.py`), включается только если задан `PROXY_CONFIG` с непустым
списком `hooks`. Форгейо шлёт push-события зеркала на `POST /webhook/forgejo` прокси:

1. Событие — пуш тега (`refs/tags/*`), тег матчит `tag_pattern` хука (SemVer).
2. Прокси клонирует conan-common (GitLab), заводит ветку `{prefix}{repo}-{version}{suffix}`.
3. Добавляет в `packages.yml` запись `{package_ref, repo, ref: <commit-sha>, recipe_path}`.
4. Коммитит и пушит ветку.

Webhook'и регистрируются в зеркалах автоматически при старте и после создания зеркала.
Подпись валидируется по `webhook_secret` (HMAC-SHA256, заголовок `X-Gitea-Signature`,
fallback `X-Forgejo-Signature`), если он задан.

## Компоненты и структура

```
docker-compose.yml     — сборка и запуск (единый контейнер Forgejo + proxy)
forgejo/
  Dockerfile           — образ: Forgejo 9 + git-proxy внутри (ЭТОТ используется compose'ом)
  app.ini              — конфиг Forgejo (sqlite, offline mode, SSH off)
  entrypoint.sh        — стартует Forgejo, создаёт admin + API-токен, затем запускает proxy.py
proxy/
  proxy.py             — HTTP-прокси, freshness-логика, git smart-HTTP проксирование
  config.py            — загрузка hooks.yml (dataclass HookConfig / ProxyConfig)
  hooks.py             — webhook-обработчик + git-workflow для conan-common
  Dockerfile           — отдельный образ только proxy (см. «Известные расхождения»)
  entrypoint.sh        — ждёт токен от Forgejo, запускает proxy.py
.env                   — секреты (в .gitignore; см. переменные ниже)
```

### Единый контейнер

`docker-compose.yml` собирает **один** образ из `forgejo/Dockerfile`, где Forgejo и git-proxy
работают в одном контейнере (коммит `65a2bd4`). `entrypoint.sh` Forgejo:
поднимает Forgejo → ждёт `/api/healthz` → создаёт admin-пользователя → выпускает API-токен
(`proxy-token`, сохраняется в `/data/gitea/proxy-token`) → стартует `proxy.py` с этим токеном.

Причина одного контейнера — **host networking** (`network_mode: host`): на хосте исходящий
трафик заворачивается через redsocks, который перехватывает только host netns, а не
Docker-bridge. Без host-режима Forgejo не достучится до github.com.

## Конфигурация

### Переменные окружения (`.env`, в `.gitignore`)

| Переменная                | Назначение                                     |
|---------------------------|------------------------------------------------|
| `FORGEJO_ADMIN_USER`      | Логин админа Forgejo (обязательно)             |
| `FORGEJO_ADMIN_PASSWORD`  | Пароль админа (обязательно)                     |
| `FORGEJO_ADMIN_EMAIL`     | Email админа (по умолчанию `admin@localhost`)  |

Прокси дополнительно читает `FORGEJO_URL`, `FORGEJO_TOKEN`, `FORGEJO_USER`,
`FORGEJO_PASSWORD` — их проставляет `entrypoint.sh`.

> **`PROXY_CONFIG` и `PROXY_PORT` из `.env` НЕ читаются.** `docker-compose.yml` в блоке
> `environment:` прокидывает в контейнер только `FORGEJO_ADMIN_*` и `PROXY_CONFIG`, причём
> `PROXY_CONFIG` там **захардкожен** (`/config/hooks.yml`), а `PROXY_PORT` не проброшен вовсе
> (внутри контейнера он по умолчанию `8080`). Чтобы сменить порт или путь конфига через
> `.env`, сначала добавьте соответствующую строку в `environment:` в `docker-compose.yml`.

### hooks.yml (опционально, для webhook→conan-common)

Монтируется в `/config` (см. `docker-compose.yml`), в git **не хранится** (содержит токены).
Значения `${VAR}` раскрываются из окружения. Структура (см. `proxy/config.py`):

```yaml
gitlab_url: https://gitlab.example.com
gitlab_token: ${GITLAB_TOKEN}
gitlab_user: some-bot
gitlab_email: bot@example.com
conan_common_repo: group/conan-common
webhook_secret: ${WEBHOOK_SECRET}   # опционально
hooks:
  - source_repo: Centimo/simd        # owner/repo на GitHub
    tag_pattern: "v{version}"         # {version} → SemVer-regex
    base_branch: main                 # база в conan-common
    branch_prefix: ""                 # префикс имени ветки (опц.)
    branch_suffix: ""                 # суффикс имени ветки (опц.)
    recipe_path: conan/conanfile.py   # путь к рецепту в записи packages.yml
```

## Запуск (локально)

```bash
cp .env.example .env   # если есть; иначе создать .env с обязательными переменными
# отредактировать FORGEJO_ADMIN_USER / FORGEJO_ADMIN_PASSWORD
docker compose up -d --build
```

- Forgejo:   `http://<host>:3000`
- git-proxy: `http://<host>:8080`

Данные (репозитории, sqlite, токен) — в `/workspace/cache/git-proxy` на хосте (см. volumes).

## Тесты

Юнит-тесты (`tests/`, pytest) покрывают чистую логику без сети и Forgejo: разбор конфига и
SemVer-паттернов (`config.py`), парсинг refs / freshness-логику / валидацию имён (`proxy.py`),
разбор имён зеркал / валидацию webhook-подписи / обработчик webhook (`hooks.py`). Все внешние
вызовы (git, Forgejo API) замоканы.

```bash
python3 -m pytest tests/ -v
```

(Корневой `test_error_response.py` — интерактивный ручной скрипт, не pytest; он исключён
через `testpaths` в `pytest.ini`.)

## Деплой на прод

Registry нет — образ собирается локально и переносится на прод-хост через `docker save`.
Полная процедура — в [DEPLOY.md](DEPLOY.md).

## Форсировать обновление зеркала вручную

На старом образе (без freshness-проверки) или для немедленной синхронизации:

```bash
curl -s -X POST -u <admin>:<pass> \
  "http://<proxy-host>:3000/api/v1/repos/gitadmin/<owner>__<repo>/mirror-sync"
```

`mirror-sync` возвращает `200/202` сразу — сама синхронизация идёт в фоне.
Проверить, что нужный коммит подтянулся:

```bash
curl -s -u <admin>:<pass> \
  "http://<proxy-host>:3000/api/v1/repos/gitadmin/<owner>__<repo>/branches/<branch>" | jq -r '.commit.id'
```

## Выбор Forgejo vs Gitea

Выбран **Forgejo** (fork Gitea): MIT+GPL, community-driven, бесплатен для коммерческого
использования; функционально для нашей задачи идентичен Gitea.

## Известные расхождения / гочи

- **Два Dockerfile.** `docker-compose.yml` использует `forgejo/Dockerfile` (Forgejo+proxy в
  одном контейнере). Отдельный `proxy/Dockerfile` описывает контейнер только с proxy и
  **compose'ом не задействован** — остался от двухконтейнерной схемы. При изменениях правьте
  оба или удалите неиспользуемый.
- **`git-config`-инжектор.** Локальные (не в git) скрипты `docker-wrapper.sh` /
  `install-docker-wrapper.sh` подменяют `docker` в PATH и прокидывают `GIT_CONFIG_COUNT/KEY_0/VALUE_0`
  в каждый `docker run` — чтобы `url.insteadOf` на прокси попадал внутрь любых контейнеров
  (например, CI-раннеров) без правки их образов. Поддерживают только одну git-config пару (индекс `_0`).
- **Историческая находка (2026-04-11):** Forgejo при `git clone` во время initial sync
  отдаёт **пустой репозиторий** (`clone` завершается успехом с warning про empty repo),
  без ошибки/блокировки. Поэтому прокси проверяет флаг `empty`, а не только факт существования.
