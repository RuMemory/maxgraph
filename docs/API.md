# 📡 Maxgraph API

Base URL: `http://<host>:8787`. Всё, кроме `/healthz`, требует `Authorization: Bearer <MEMORY_TOKEN>`.

## MCP (`POST /mcp`, streamable-http)

| Инструмент | Аргументы | Описание |
|---|---|---|
| `create_entities` | `entities: [{name, entityType?, title?, tags?, observations?}]` | Создать сущности (существующее имя → merge observations) |
| `create_relations` | `relations: [{from, to, relationType}]` | Создать связи (дубликаты пропускаются) |
| `add_observations` | `observations: [{entityName, contents[]}]` | Добавить факты (дедупликация) |
| `delete_entities` | `entityNames: []` | Удалить сущности и их связи |
| `delete_observations` | `deletions: [{entityName, observations[]}]` | Удалить конкретные факты |
| `delete_relations` | `relations: [{from, to, relationType}]` | Удалить связи |
| `read_graph` | — | Весь граф |
| `search_nodes` | `query`, `limit?` (default 20) | Ранжированный поиск (Ф6): score/snippet/highlights; пустой query — все сущности |
| `open_nodes` | `names: []` | Открыть сущности по именам + связи между ними |
| `list_timeline` | `limit?` | Последние события журнала |
| `update_entity` | `name, title?, entityType?, tags?, status?` | Обновить метаданные сущности; `status` — для bug (`open`/`fixed`) и task (`open`/`done`) |
| `session_start` | `project` (обязателен), `budget_chars?` (default 10000) | Старт сессии: бюджетный `context_block` + `meta` |
| `task_add` | `title`, `note?`, `project?` | В2.1: открытое дело «на потом» → task-сущность `task-<slug(title)>` (status `open`; коллизия имени → суффикс `-2`) |
| `task_done` | `name`, `project?` | В2.1: закрыть задачу (`status=done` + событие `task_done` в пульс; повтор — `already: true`) |
| `task_list` | `project?` | В2.1: открытые задачи (status≠done), recency-first; без `project` — все открытые |

**session_start (Ф2)**: возвращает `{"context_block", "meta"}`. `context_block` — готовый к вставке в промпт текст с секциями по приоритету: `# Проект: <slug>` (title + до 5 ключевых фактов) → `## Решения владельца` (entityType `decision`, recency-first, дословно, ≤50 строк) → `## Открытые баги` (entityType `bug` без закрывающего статуса, ≤30 строк) → `## Открытые задачи` (entityType `task`, status≠`done`, ≤10 строк: title + первый факт; В2.1) → `## Прошлые сессии` (entityType `session`, ≤10: имя + первые 2 факта) → `## Недавние события` (≤15 событий пульса проекта). Бюджет жёсткий: `len(context_block) ≤ budget_chars` всегда; усечение построчное, снизу вверх (порядок жертвования: events → sessions → tasks → bugs → decisions), с маркером «… (ещё N, усечено бюджетом)». `meta`: `{project, budget_chars, used_chars, sections: {project|decisions|bugs|tasks|sessions|events: {items, total}}, truncated}`; `items < total` без `truncated=true` — сработал кэп секции, не бюджет. Пустой проект → блок-подсказка «начни с create_entities»; без `project` — ошибка (весь граф не читается). Чтение: пульс не пишется. В2.3: внутри секций решений/багов/задач порядок — актуальность (свежесть × ACT-R множитель частоты обращений, ≤+25%; `ACTR_MAX_BOOST=0` — чистая свежесть, как раньше), а сущности, попавшие в блок, учитываются как обращения.

**Проекты (Ф1)**: все инструменты принимают `project` (kebab-slug, регистр приводится к нижнему; в `session_start` он обязателен, в остальных — опционален). `read_graph`/`search_nodes`/`list_timeline` — ещё и `all_projects: bool` (явный кросс-поиск по всем проектам). Без `project` — плоский граф, поведение как до Ф1.

**Поиск (Ф6)**: непустой `query` — ранжированный FTS-lite (BM25-подобный скоринг по полям `name ×3 / title ×2 / tags ×1.5 / observations ×1` + фразовый бонус + recency ≤10%, стабильная сортировка score desc → updatedAt desc → name). У сущностей ответа + `score` (>0), `snippet` (лучший фрагмент observation, ≤160 символов), `highlights` — позиции совпадений `[[start, end], …]` внутри snippet (без HTML, безопасно для esc()). `limit` — топ-N (default 20). Фильтры `type=` (после нормализации, Ф3) и `project`/`all_projects` (Ф1) комбинируются — применяются к корпусу до скоринга. Пустой `query` — старое поведение: все сущности с фильтрами, без `score`/`snippet`/`highlights`. Запрос без извлекаемых терминов (пунктуация, CJK) — фоллбек на подстрочный поиск, тоже без FTS-полей.

**Семантический поиск (В2.2, опционально)**: при заданных env `EMBED_URL` (OpenAI-compatible `/embeddings` endpoint) + `EMBED_KEY` поиск становится гибридным: `score = (1−w)·fts_norm + w·cos_norm`, где `w = HYBRID_VECTOR_WEIGHT` (default `0.6` — по бенчу Q4, `bench/results-2026-10-10.md`), `fts_norm = fts/max(fts)` по кандидатам, `cos_norm` — mean-anchored по кандидатам запроса: `(cos−mean)/(max−mean)` обрезанное в [0,1] ((cos+1)/2 сжимало рабочий диапазон e5 в 0.85–0.95 и сигнал тонул). Косинус считается по ВСЕМ кандидатам при корпусе ≤2000 (full-scan: парафраз без лексического пересечения поднимается — у непопавших в FTS `fts=0`, бленд даёт `w·cos_norm`; замер на n=2000: FTS ~10мс → hybrid ~26мс, ×2.5 чистым stdlib), при большем корпусе — только по FTS top-200 (предфильтр, ~5–10мс); сущность без вектора получает нейтральный 0.5 (не штраф); равные косинусы → 0.5 всем (дискриминации нет). В гибридном режиме `score` ответа — blended 0..1. Вектора сущностей (основной текст: `name`+`title`+первые 3 факта, ≤1000 символов; с v0.20.2 факты за пределами трёх упаковываются в ≤4 доп. окна ≤800 символов — скор сущности = max-косинус по её векторам, мега-сущности больше не теряют поздние факты) считает фоновый пополнитель (батчи ≤32, период `EMBED_REFRESH_SEC` default 300с, кап `EMBED_DAILY_CAP` default 200k токенов/день по оценке len/4) в `data/embeddings.json` (атомарная запись, инвалидация по `updatedAt`; сущности удалённых из графа выметаются). Запрос эмбеддится синхронно с LRU-кэшем 128 по (query, model); таймаут `EMBED_TIMEOUT` (default 10с). Без `EMBED_URL`/`EMBED_KEY`, при сбое сети или исчерпании капа — автоматический фоллбек на чистый FTS (поведение 0.14.x); изоляция проектов сохраняется (косинусы считаются только по отфильтрованному корпусу). READONLY-зеркало: пополнитель не стартует, гибридный поиск работает, если завезти `embeddings.json` ресинком. `EMBED_MODEL` (default `text-embedding-v3`) фиксируется отпечатком `_model` в `embeddings.json`: файл с чужим или отсутствующим (старый формат) отпечатком читается пустым (один warning на сессию) — смена модели инвалидирует вектора, пополнитель пересчитывает их сам. Zero-config: `embed.configure_runtime(url, key, model)` включает/выключает embed поверх env без рестарта процесса (приоритет над env до рестарта; поиск подхватывает на следующий вызов, уже запущенный пополнитель — на следующий тик). Hosted-вариант для Pro: `POST /api/relay/config` (магазин) выдаёт relay-конфиг по ключу активации, клиент подтягивает его сам (активация + суточный watchdog) — настраивать ничего не надо.

**Автообновления (v0.20.0; механика watchtower-style с v0.22.0)**: sidecar-контейнер `maxgraph-updater` раз в час (дефолт `CHECK_INTERVAL=3600`, v0.32.2; floor 300с) сверяет `GET /api/version` магазина с версией работающего контейнера; при новом релизе и `AUTO_UPDATE=1` (default) скачивает `GET /api/releases/latest.tar.gz` + `.sha256` + `.sig`, проверяет sha256 и **ed25519-подпись** публичным ключом, вшитым в образ (`services/updater/release.pub.pem`; ключ подписи хранится только на сервере магазина), проверяет архив (только файлы/каталоги, без абсолютных/`..` путей), бэкапит `data/` целевого контейнера через `docker cp` в named volume `updater-state` (ротация 5), собирает образ прямо из tarball'а (`docker build` — контекст уходит демону потоком), пересоздаёт контейнер **clone-recreate** по `docker inspect` (env/mounts/ports/labels/сеть клонируются — пути хоста не участвуют, поэтому работает на Docker Desktop Windows/macOS и Linux; `HOST_REPO_DIR` упразднён), healthcheck 120с + сверка VERSION, провал → авто-откат (rollback-контейнер + данные из бэкапа). Self-update самого updater'а — через одноразовый helper-контейнер (recreate собственного контейнера изнутри невозможен — stop убивает PID-namespace). Итог — `update_state.json` на volume `updater-state`. Эндпоинты релизов публичные (rate-limit 30/мин/IP), отдают файлы из `RELEASES_DIR` (default `data/releases/`) только для текущей VERSION магазина; 404 = релиз не упакован. `AUTO_UPDATE=0` — только лог «доступна версия»; `RUN_ONCE=1` — один цикл и выход (для cron/ручного запуска).

**ACT-R reinforcement (В2.3, default ON)**: память сама учится, что важно — сущности, к которым агент обращается чаще и недавнее, всплывают выше. «Осмысленные» чтения учитываются как обращения: `search_nodes` (top-20 выдачи), `open_nodes` (найденные), `session_start` (сущности, попавшие в `context_block` после бюджетной обрезки). `read_graph` и пустой `query` (дамп корпуса) НЕ учитываются. Счётчики in-memory `{name: {n, last_ts}}` — НЕ в `memory.json` (иначе каждый поиск был бы fsync'ом и сдвигал `updatedAt`); атомарный flush (tmp+replace) в `data/access.json` раз в `ACTR_FLUSH_SEC` (default 300с, фоновый daemon-поток) и при shutdown (atexit); загрузка при старте, битый/отсутствующий файл → с нуля (fail-open). Множитель ранга: `1 + min(ln(1+n) · 0.5^(дней_с_последнего/ACTR_HALF_LIFE_DAYS), 1.0) · ACTR_MAX_BOOST` — максимум +25% при default `ACTR_MAX_BOOST=0.25` и полураспаде 14 дней; `n=0` → ровно ×1.0 (нейтраль). Применяется в `search_nodes`/`/api/search` (к итоговому скору — FTS или blended В2.2) и в сортировке внутри секций решений/багов/задач `session_start` (актуальность = свежесть × частота; сессии — чистая хронология). `ACTR_MAX_BOOST=0` — полное отключение (поведение byte-identical: `rank` вызывается без `actr_fn`). Изоляция проектов сохраняется: счётчики глобальны по имени, но имена сущностей глобально уникальны, а буст применяется после фильтра корпуса (Ф1/Ф3). Пульс и события не пишутся (чтение/фоновый flush — без спама). READONLY-зеркало: flush не стартует, буст работает по завезённому ресинком `access.json`.

## REST

| Метод и путь | Назначение |
|---|---|
| `GET /` | Дашборд (HTML) |
| `GET /healthz` | Healthcheck (без токена) |
| `GET /api/graph` | Весь граф |
| `GET /api/search?q=&type=&limit=` | Ранжированный поиск (Ф6): `limit` default 20, cap 500; у сущностей + `score`, `snippet`, `highlights`; с `EMBED_URL`+`EMBED_KEY` — гибрид FTS+косинус (В2.2, blended `score` 0..1) |
| `GET /api/timeline?limit=` | События (новые первыми) |
| `GET /api/stats` | Счётчики, версия, аптайм |
| `POST /api/entities` | `{"entities":[...]}` |
| `POST /api/entity/update` | `{"name","title"?,"entityType"?,"tags"?,"status"?}` |
| `POST /api/task/add` | В2.1: `{"title","note"?,"project"?}` → task-сущность `task-<slug(title)>` |
| `POST /api/task/done` | В2.1: `{"name","project"?}` → `status=done` + событие `task_done` |
| `GET /api/tasks` | В2.1: открытые задачи (status≠`done`, recency-first); `?project=<slug>` — только этот |
| `POST /api/observations` | `{"observations":[...]}` |
| `POST /api/relations` | `{"relations":[...]}` |
| `POST /api/delete/entities` | `{"entityNames":[...]}` |
| `POST /api/delete/observations` | `{"deletions":[...]}` |
| `POST /api/delete/relations` | `{"relations":[...]}` |
| `POST /api/ingest` | Приёмник захват-хуков (Ф5): `{"events":[{"hook","project"?,"ts","data"}]}` — до 100 событий, тело ≤1MB; идемпотентно по content-hash (повтор → `{"dedup":n}`); `SessionEnd` → сессия дня на проект, `data.open_tasks` (список строк, В2.1) → открытые task-сущности; 400 невалидный батч, 401 без токена, 402 trial expired, 403 READONLY, 413 тело >1MB, 429 rate-limit 60/мин с IP (`Retry-After: 60`, Ф8). Клиент: `hooks/maxgraph_hook.py` (hooks/README.md) |

**Проекты (Ф1)**: GET-эндпоинты памяти принимают `?project=<slug>` (`/api/graph`, `/api/search`, `/api/timeline`, `/api/stats`, `/api/export`; плюс `all_projects=1` у graph/search/timeline/stats), POST — поле `"project"` в теле. Невалидный slug → 400. Миграция легаси-данных: `deploy/migrate-projects.py --project kimi`.

## Файлы данных (volume `/data`)

- `memory.json` — состояние графа (NDJSON); у сущности опциональное поле `project` (kebab-slug)
- `events.jsonl` — журнал событий; у события опциональное поле `project`
- `embeddings.json` — (опционально, В2.2) вектора сущностей `{"_model": отпечаток модели, name: {"v": [...], "u": updatedAt}}`; пишется фоновым пополнителем при заданных `EMBED_URL`+`EMBED_KEY`
- `embed.state.json` — (опционально, В2.2) дневной счётчик токенов эмбеддингов
- `access.json` — (В2.3) ACT-R счётчики обращений `{name: {"n": count, "t": last_ts}}`; пишется фоновым flush (раз в `ACTR_FLUSH_SEC`) и при shutdown; битый/отсутствующий → счёт с нуля
- `dashboard.html` — (опционально) переопределяет встроенный дашборд без пересборки образа
