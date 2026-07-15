# git-proxy

HTTP-прокси перед Forgejo, который **по требованию** зеркалирует публичные git-репозитории
**любых** allowlist-хостов (github.com, gitlab.com, self-hosted GitLab и т.п.) и на каждый
`git clone`/`fetch` гарантирует их свежесть. Плюс webhook-механизм, который на пуш релизного
тега в зеркало запускает произвольную внешнюю команду — сам прокси не знает ни про conan, ни
про какую-либо конкретную цель.

## Зачем

Ускорить и сделать надёжнее CI-сборки, которым нужно клонировать репозитории с внешних
git-хостов. В частности — для корректного RREV в `conan graph build-order` + lockfile-подходе,
где клонируются репозитории с `export_sources()` (protobuf, Simd, onnxruntime и т.п.).

CI обращается к прокси вместо исходного хоста. Прокси держит локальные зеркала в Forgejo,
поэтому клоны идут из локальной сети, а не с внешнего хоста.

## Как это работает

Хост источника кодируется **первым сегментом пути**. Клиент подменяет каждый исходный хост
на адрес прокси с этим хостом в префиксе (по одной `insteadOf`-паре на хост):

```bash
git config --global url."http://<proxy-host>:8080/github.com/".insteadOf "https://github.com/"
git config --global url."http://<proxy-host>:8080/gitlab.com/".insteadOf "https://gitlab.com/"
```

После этого `git clone https://gitlab.com/group/sub/repo` фактически идёт на
`http://<proxy-host>:8080/gitlab.com/group/sub/repo`. Первый сегмент пути — хост источника
(должен быть в allowlist `PROXY_SOURCES`), остаток — путь репозитория **любой глубины**
(включая GitLab-подгруппы). Прокси обрабатывает запрос так:

| Состояние зеркала в Forgejo         | Действие прокси                                              |
|-------------------------------------|-------------------------------------------------------------|
| Не существует                       | Создаёт зеркало (Forgejo API `POST /repos/migrate`), возвращает git-`ERR` → клиент повторяет запрос |
| Существует, но пустое (`empty:true`) | Запускает `mirror-sync`, ждёт заполнения; пока не готово — git-`ERR` (клиент повторяет) |
| Существует и заполнено              | Проксирует запрос в Forgejo; клиент получает данные          |

Push (`POST .../git-receive-pack`) отклоняется с `403` — зеркала read-only. Запрос на хост
вне `PROXY_SOURCES` (или с недопустимым сегментом пути) — `400`, зеркало не создаётся.

### Проверка свежести (freshness)

Механизм — синхронизация зеркала **по требованию**. Вместо сравнения ссылок (refs) зеркала
с оригиналом прокси просто просит Forgejo выполнить `mirror-sync` перед тем, как отдать данные
клиенту, а результат этой синхронизации кратко кешируется, чтобы не дёргать `mirror-sync` на
каждый запрос. Реализовано в классе `MirrorFreshness` (`proxy/proxy.py`), метод `ensure_synced`.

Срабатывает на двух типах запросов — оба входят в любой `clone`/`fetch`:

- `GET /<host>/<path>/info/refs?service=git-upload-pack` — анонс ссылок (refs) в начале операции.
- `POST /<host>/<path>/git-upload-pack` — сама передача объектов, где клиент указывает `want <sha>`,
  в том числе произвольный commit SHA, а не только конец ветки.

Логика `ensure_synced`:

1. **Кеш свежести.** Если зеркало успешно синхронизировалось за последние `PROXY_SYNC_TTL`
   секунд (по умолчанию 30) — синхронизация не запускается, отдаём как есть.
2. **Дедупликация (single-flight).** Если синхронизация этого репозитория уже идёт — параллельные
   запросы не создают вторую, а объединяются с первой (в режиме `wait` — ждут её завершения,
   в режиме `async` — сразу отдают текущее состояние зеркала).
3. **Запуск синхронизации** через Forgejo API `POST /mirror-sync`. Дальнейшее поведение зависит
   от режима `PROXY_SYNC_MODE`:
   - **`wait`** (по умолчанию) — прокси дожидается фактического завершения синхронизации, опрашивая
     поле `mirror_updated` в `GET /repos/{user}/{name}`, пока оно не изменится (таймаут —
     `SYNC_WAIT_TIMEOUT`, 120 с). Данные клиенту гарантированно актуальны, но первый запрос за
     `PROXY_SYNC_TTL` получает задержку на время синхронизации.
   - **`async`** — прокси запускает синхронизацию и сразу отдаёт то, что уже есть в зеркале
     (оно к этому моменту точно не пустое — см. таблицу выше). Задержки нет, но данные могут
     отставать от GitHub на время до `PROXY_SYNC_TTL`.
4. **Fail-open.** Любая ошибка обращения к Forgejo API (сеть, таймаут, не-200 ответ) — логируется
   как error и не пробрасывается наружу; прокси всё равно проксирует запрос (лучше отдать чуть
   устаревшее зеркало, чем упасть).

Чтобы зеркало могло отдать произвольный commit SHA (не только концы веток и тегов), в Forgejo
включена опция `uploadpack.allowAnySHA1InWant` (секция `[git.config]` в `forgejo/app.ini`) —
без неё `git fetch <произвольный-sha>` против зеркала завершался бы ошибкой, даже если нужный
коммит физически есть в репозитории.

> Раньше свежесть проверялась сравнением ссылок (refs) зеркала с оригиналом на GitHub через
> `git ls-remote`, и синхронизация запускалась только при расхождении. Текущая версия вместо
> сравнения полагается на прямой запрос синхронизации к Forgejo.

### Схема имён зеркал

Имя зеркала в Forgejo — детерминированный **слаг + короткий хеш** от канонического ключа
`host/path`: `slug = re.sub('[^A-Za-z0-9]+','-', "host/path")` (усечённый до 88 символов) плюс
`-{8 hex}` от `sha256(host/path)`. Владелец — админ Forgejo (`gitadmin` на проде). Пример:
`github.com/Centimo/simd` → `gitadmin/github-com-Centimo-simd-0c2b132c`.

Хеш-суффикс делает имя коллизионно-безопасным по хосту и вложенности (`github.com/a/b`,
`gitlab.com/a/b`, `github.com/a/b/c` дают разные имена) и структурно исключает forbidden-имена
Forgejo (`.`/`..`, хвосты `.git`/`.wiki`/… — точки заменены на `-`). Длина ограничена ~97 символами.

Обратное сопоставление зеркало → источник **не** делается разбором имени: источник берётся из
поля `original_url` репозитория (Forgejo заполняет его из `clone_addr` при миграции и сериализует
как в `GET /repos/{user}/{name}`, так и внутри payload вебхука). См. `proxy/source.py`.

## Интеграция с GitLab CI

Задача — чтобы `git clone https://github.com/...` / `https://gitlab.com/...` из джоб (и клон
сабмодулей, указывающих на эти хосты) уходил на прокси. Подмену задаёт git-опция
`url.<base>.insteadOf`. Так как хост кодируется в путь, **на каждый хост-источник нужна своя
пара** `insteadOf`. Удобнее всего пробрасывать их **через переменные окружения**
`GIT_CONFIG_COUNT/KEY_i/VALUE_i` — git читает их глобально, поэтому подмена действует и на фазу
`get_sources`, и на инициализацию сабмодулей (до `before_script`), а не только на явные
`git clone` в скрипте. Требуется git ≥ 2.31 на раннере.

Значения переменных (общие для всех вариантов ниже; здесь github.com + gitlab.com):

```
GIT_CONFIG_COUNT=2
GIT_CONFIG_KEY_0=url.http://<proxy-host>:8080/github.com/.insteadOf
GIT_CONFIG_VALUE_0=https://github.com/
GIT_CONFIG_KEY_1=url.http://<proxy-host>:8080/gitlab.com/.insteadOf
GIT_CONFIG_VALUE_1=https://gitlab.com/
```

> **Важно про сабмодули.** Если сабмодуль ссылается на хост, которого нет в `PROXY_SOURCES`
> (и в `insteadOf` выше), возможны два исхода: если его `insteadOf` не переписал — клон идёт
> напрямую; если переписал — прокси вернёт `400` и клон упадёт. Перечислите в `PROXY_SOURCES`
> и в `insteadOf` **все** хосты, на которые ссылаются сабмодули CI.

### На весь раннер (`config.toml`)

Подмена применяется ко всем джобам раннера. Для docker-executor'а:

```toml
[[runners]]
  [runners.docker]
    environment = [
      "GIT_CONFIG_COUNT=2",
      "GIT_CONFIG_KEY_0=url.http://<proxy-host>:8080/github.com/.insteadOf",
      "GIT_CONFIG_VALUE_0=https://github.com/",
      "GIT_CONFIG_KEY_1=url.http://<proxy-host>:8080/gitlab.com/.insteadOf",
      "GIT_CONFIG_VALUE_1=https://gitlab.com/",
    ]
```

Для shell-executor'а — тот же список, но на уровне самого `[[runners]]`.

### На один проект (`.gitlab-ci.yml`)

Если раннер общий и трогать `config.toml` нельзя:

```yaml
variables:
  GIT_CONFIG_COUNT: "2"
  GIT_CONFIG_KEY_0: "url.http://<proxy-host>:8080/github.com/.insteadOf"
  GIT_CONFIG_VALUE_0: "https://github.com/"
  GIT_CONFIG_KEY_1: "url.http://<proxy-host>:8080/gitlab.com/.insteadOf"
  GIT_CONFIG_VALUE_1: "https://gitlab.com/"
```

### Вложенные контейнеры (`docker run` внутри джобы)

Если джоба сама запускает контейнеры (сборка внутри `docker run`, DinD), переменные
`GIT_CONFIG_*` из окружения джобы **не попадают** в эти вложенные контейнеры автоматически.
На этот случай в репозитории есть `docker-wrapper.sh` — он подменяет `docker` в PATH и на каждый
`docker run` заново прокидывает `-e GIT_CONFIG_COUNT` и по паре `-e GIT_CONFIG_KEY_i
-e GIT_CONFIG_VALUE_i` на каждый индекс `0..GIT_CONFIG_COUNT-1` (падает с ошибкой при конфликте,
если переменная уже передана вручную). Установка на хост раннера — `install-docker-wrapper.sh`
(кладёт враппер в `/usr/local/bin/docker`).

### Хост раннера напрямую

Для клонов вне CI (или как запасной вариант для shell-раннера) — системный git-config хоста
(по паре на хост):

```bash
sudo git config --system url."http://<proxy-host>:8080/github.com/".insteadOf "https://github.com/"
sudo git config --system url."http://<proxy-host>:8080/gitlab.com/".insteadOf "https://gitlab.com/"
```

## Webhook → внешняя команда

Отдельная функция (`proxy/hooks.py`), включается только если задан `PROXY_CONFIG` с непустым
списком `hooks`. Форгейо шлёт push-события зеркала на `POST /webhook/forgejo` прокси:

1. Событие — пуш тега (`refs/tags/*`), тег матчит `tag_pattern` хука (SemVer).
2. Прокси разыменовывает аннотированный тег до commit-SHA и запускает `command` этого хука
   во временном рабочем каталоге, передавая параметры релиза через переменные окружения.
3. Прокси не делает больше ничего: вся логика (клонировать что-то, править файл, пушить ветку
   и т.п.) живёт целиком в этой внешней команде.

Webhook'и регистрируются в зеркалах автоматически при старте и после создания зеркала.
Подпись валидируется по `webhook_secret` (HMAC-SHA256, заголовок `X-Gitea-Signature`,
fallback `X-Forgejo-Signature`), если он задан.

### Переменные окружения команды

Команда наследует окружение прокси, плюс `env` этого хука, плюс:

| Переменная               | Значение                                                    |
|--------------------------|-------------------------------------------------------------|
| `GIT_PROXY_SOURCE_REPO`  | Канонический ключ `host/path` (например `github.com/Centimo/simd`) |
| `GIT_PROXY_SOURCE_HOST`  | Хост источника (например `github.com`)                      |
| `GIT_PROXY_SOURCE_URL`   | Clone-URL источника (`https://github.com/Centimo/simd.git`) |
| `GIT_PROXY_MIRROR`       | Полное имя зеркала в Forgejo (`gitadmin/<слаг>-<8hex>`)      |
| `GIT_PROXY_TAG`          | Исходный тег (`v1.2.3`)                                      |
| `GIT_PROXY_VERSION`      | Версия из тега по `tag_pattern` (`1.2.3`)                   |
| `GIT_PROXY_COMMIT_SHA`   | Commit-SHA, на который указывает тег (разыменованный)       |
| `GIT_PROXY_WORKDIR`      | Временный рабочий каталог команды (`cwd`; удаляется после)  |

Команда выполняется в фоне (ошибка/таймаут только логируется, на HTTP-ответ webhook не влияет —
Forgejo сразу получает `202`). Каталог `GIT_PROXY_WORKDIR` удаляется по завершении.

## Компоненты и структура

```
docker-compose.yml     — сборка и запуск (единый контейнер Forgejo + proxy)
forgejo/
  Dockerfile           — образ: Forgejo 9 + git-proxy внутри (ЭТОТ используется compose'ом)
  app.ini              — конфиг Forgejo (sqlite, offline mode, SSH off)
  entrypoint.sh        — стартует Forgejo, создаёт admin + API-токен, затем запускает proxy.py
proxy/
  proxy.py             — HTTP-прокси, проверка свежести зеркал, git smart-HTTP проксирование
  source.py            — модель источника: allowlist хостов, разбор путей, имена зеркал
  config.py            — загрузка hooks.yml (dataclass HookConfig / ProxyConfig)
  hooks.py             — webhook-обработчик + запуск внешней команды хука
  Dockerfile           — отдельный образ только proxy (см. «Известные расхождения»)
  entrypoint.sh        — ждёт токен от Forgejo, запускает proxy.py
.env                   — секреты (в .gitignore; см. переменные ниже)
```

### Единый контейнер

`docker-compose.yml` собирает **один** образ из `forgejo/Dockerfile`, где Forgejo и git-proxy
работают в одном контейнере. `entrypoint.sh` Forgejo:
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
| `PROXY_PORT`              | Порт, на котором слушает прокси (по умолчанию `8080`) |
| `PROXY_CONFIG`            | Путь к hooks.yml внутри контейнера (по умолчанию `/config/hooks.yml`) |
| `PROXY_SYNC_MODE`         | Режим синхронизации зеркала: `wait` (ждать завершения, по умолчанию) или `async` (отдать зеркало сразу) |
| `PROXY_SYNC_TTL`          | Сколько секунд после успешной синхронизации зеркало считается свежим (по умолчанию `30`) |
| `PROXY_SOURCES`           | Allowlist хостов-источников через запятую; элемент `host` или `host=<baseURL>` (по умолчанию `github.com`) |

Прокси дополнительно читает `FORGEJO_URL`, `FORGEJO_TOKEN`, `FORGEJO_USER`,
`FORGEJO_PASSWORD` — их проставляет `entrypoint.sh`.

> **Смена порта.** Достаточно задать `PROXY_PORT` в `.env` (`docker-compose.yml` пробрасывает
> его в контейнер) и пересоздать контейнер: `proxy.py` слушает на нём, а webhook-callback URL
> внутри Forgejo (`hooks.py`) строится из того же значения — подхватится автоматически.
> Единственное, что нужно поправить вручную, — клиентский `url.insteadOf` (он вне контейнера):
> укажите там тот же порт.

### hooks.yml (опционально, для webhook-хуков)

Монтируется в `/config` (см. `docker-compose.yml`), в git **не хранится** (может содержать
секреты). Значения `${VAR}` раскрываются из окружения. Структура (см. `proxy/config.py`):

```yaml
webhook_secret: ${WEBHOOK_SECRET}    # опционально; включает проверку HMAC-подписи
hooks:
  - source: github.com/Centimo/simd  # host/path источника, любой глубины (обязательно; хост ∈ PROXY_SOURCES)
    tag_pattern: "v{version}"         # {version} → SemVer-regex (обязательно)
    command: /config/hooks/on-release.sh   # что запустить на совпавший тег (обязательно)
    timeout: 300                      # секунд до убийства команды (опц., по умолчанию 300)
    env:                              # доп. переменные окружения команды (опц.)
      TARGET_REPO: group/conan-common
      GITLAB_TOKEN: ${GITLAB_TOKEN}
```

`command` может быть:

- строкой — выполняется через `/bin/sh -c` (доступны `&&`, пайпы, `cd` и т.п.);
- списком argv — выполняется напрямую, без шелла: `command: ["python3", "on-release.py"]`.

Сама команда (скрипт) в git-proxy не входит — она специфична для вашей задачи и живёт рядом
с `hooks.yml` (например, смонтированной в `/config`). Внутри доступны переменные `GIT_PROXY_*`
(см. «Переменные окружения команды») и всё, что задано в `env`.

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

Юнит-тесты (`tests/`, pytest) покрывают чистую логику без сети и Forgejo: имена зеркал /
разбор путей и `host/path`-источников / allowlist (`source.py`), разбор конфига и
SemVer-паттернов (`config.py`), синхронизацию по требованию (`ensure_synced`) / оркестрацию
запросов (`proxy.py`), восстановление источника из `original_url` / валидацию webhook-подписи /
обработчик webhook (`hooks.py`). Все внешние вызовы (git, Forgejo API) замоканы.

```bash
python3 -m pytest tests/ -v
```

(Корневой `test_error_response.py` — интерактивный ручной скрипт, не pytest; он исключён
через `testpaths` в `pytest.ini`.)

## Принудительное обновление зеркала вручную

Имя зеркала (`<слаг>-<8hex>`) можно найти в Forgejo UI или через API. Для немедленной
синхронизации:

```bash
curl -s -X POST -u <admin>:<pass> \
  "http://<proxy-host>:3000/api/v1/repos/gitadmin/<mirror-name>/mirror-sync"
```

`mirror-sync` возвращает `200/202` сразу — сама синхронизация идёт в фоне.
Проверить, что нужный коммит подтянулся:

```bash
curl -s -u <admin>:<pass> \
  "http://<proxy-host>:3000/api/v1/repos/gitadmin/<mirror-name>/branches/<branch>" | jq -r '.commit.id'
```

## Выбор Forgejo vs Gitea

Выбран **Forgejo** (fork Gitea): MIT+GPL, community-driven, бесплатен для коммерческого
использования; функционально для нашей задачи идентичен Gitea.

## Известные расхождения и подводные камни

- **Два Dockerfile.** `docker-compose.yml` использует `forgejo/Dockerfile` (Forgejo+proxy в
  одном контейнере). Отдельный `proxy/Dockerfile` описывает контейнер только с proxy и
  **compose'ом не задействован** — остался от двухконтейнерной схемы. При изменениях правьте
  оба или удалите неиспользуемый.
- **Историческая находка (2026-04-11):** Forgejo при `git clone` во время initial sync
  отдаёт **пустой репозиторий** (`clone` завершается успехом с warning про empty repo),
  без ошибки/блокировки. Поэтому прокси проверяет флаг `empty`, а не только факт существования.
