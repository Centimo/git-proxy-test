# Деплой git-proxy

Здесь и далее `<PROXY_HOST>` — адрес прод-хоста (подставить свой). Registry нет — образ
собирается локально и переносится по SSH через `docker save | docker load`.

- Forgejo:   `http://<PROXY_HOST>:3000`  (admin: `gitadmin`)
- git-proxy: `http://<PROXY_HOST>:8080`
- Данные на хосте: `/workspace/cache/git-proxy` (репозитории, sqlite, `proxy-token`)

## Схема

Единый образ (Forgejo + proxy в одном контейнере) собирается из `forgejo/Dockerfile`.
`docker-compose.yml` тегирует его при `up --build`; для переноса на прод пересобираем
тот же образ явным тегом и грузим на удалённый хост.

## Процедура

### 1. Собрать образ локально

Host networking при сборке нужен, чтобы `apk`/`pip` ходили в интернет через redsocks:

```bash
cd /workspace/projects/devops/git-proxy
DOCKER_BUILDKIT=1 docker build --network host -f forgejo/Dockerfile -t git-proxy-forgejo .
```

### 2. Перенести образ на прод

```bash
# со сжатием и прогрессом (нужен pv на локальной машине):
docker save git-proxy-forgejo | pv | gzip | ssh <PROXY_HOST> 'gunzip | docker load'

# без прогресса:
docker save git-proxy-forgejo | gzip | ssh <PROXY_HOST> 'gunzip | docker load'

# без сжатия (быстрее по CPU, больше по сети):
docker save git-proxy-forgejo | ssh <PROXY_HOST> 'docker load'
```

### 3. Перезапустить на проде

На `<PROXY_HOST>`, в каталоге с `docker-compose.yml` и `.env`:

```bash
ssh <PROXY_HOST> 'cd <путь-к-git-proxy> && docker compose up -d'
```

> Точный путь к git-proxy на `<PROXY_HOST>` в истории не зафиксирован — уточнить на месте
> (`ssh <PROXY_HOST> 'docker inspect forgejo --format "{{ range .Mounts }}{{ .Source }}{{ println }}{{ end }}"'`
> покажет смонтированные каталоги; `docker-compose.yml` лежит рядом с конфигом).

Данные переживают пересоздание контейнера — они в volume `/workspace/cache/git-proxy` на хосте,
не в образе. Admin-пользователь и API-токен при первом старте уже созданы и сохранены в
`/data/gitea/proxy-token`, повторно не пересоздаются.

## Проверка после деплоя

```bash
# Forgejo жив:
curl -sf http://<PROXY_HOST>:3000/api/healthz

# proxy отвечает (info/refs уже существующего зеркала):
curl -s "http://<PROXY_HOST>:8080/Centimo/onnxruntime/info/refs?service=git-upload-pack" | head -c 200

# список зеркал:
curl -s -u gitadmin:<pass> "http://<PROXY_HOST>:3000/api/v1/repos/search?limit=100" | jq -r '.data[].full_name'

# логи:
ssh <PROXY_HOST> 'docker logs --tail 100 forgejo'
```

## Принудительное обновление зеркала

Нужно на **старом** образе (без проверки свежести на каждый запрос, коммит до `af2ef85`) либо для
немедленной синхронизации без ожидания расписания Forgejo:

```bash
# дёрнуть sync:
curl -s -X POST -u gitadmin:<pass> \
  "http://<PROXY_HOST>:3000/api/v1/repos/gitadmin/<owner>__<repo>/mirror-sync"

# проверить SHA нужной ветки в зеркале:
curl -s -u gitadmin:<pass> \
  "http://<PROXY_HOST>:3000/api/v1/repos/gitadmin/<owner>__<repo>/branches/<branch>" | jq -r '.commit.id'

# сравнить с оригиналом на GitHub:
git ls-remote https://github.com/<owner>/<repo>.git refs/heads/<branch>

# проверить наличие конкретного коммита в кэше зеркала:
curl -s -u gitadmin:<pass> \
  "http://<PROXY_HOST>:3000/api/v1/repos/gitadmin/<owner>__<repo>/git/commits/<sha>" -o /dev/null -w '%{http_code}\n'
```

`mirror-sync` возвращает `200/202` сразу; синхронизация идёт в фоне. Для крупных репозиториев
(onnxruntime и т.п.) после запроса подождать и перепроверить SHA — одного `sleep 5` может не хватить.
