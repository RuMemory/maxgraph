# 🔗 MaxGraph Pro: от покупки до живой памяти

Ты купил MaxGraph. Эта инструкция проведёт тебя от письма с ключом до неонового графа, на котором вспыхивают знания твоего агента. Четыре шага, ≈10 минут.

---

## 1. Что ты получаешь после покупки

На email (и на странице успеха) приходит главный артефакт:

```
Оплата на rezon-ai.ru
        │
        ▼  email / страница успеха
┌────────────────────────────────────────────────────────────┐
│ License key  mg_… (длинная подписанная строка)             │
│ → вводится В ЛОКАЛЬНЫЙ СЕРВЕР → открывает Pro-функции      │
└────────────────────────────────────────────────────────────┘
        │
        ▼
  Твой сервер (локально): localhost:8787, память в data/
```

- **License key `mg_…`** — единственное, что нужно. Привязан к email покупки и тарифу.
- Если в письме вместо ключа **код `MG-XXXX-XXXX`** — это резервный фолбэк: введи его на [rezon-ai.ru](https://rezon-ai.ru) в секции «Активировать код», и сайт покажет твой license key.

Сохрани ключ в менеджер паролей сразу. Поехали.

---

## 2. Установка локального сервера

Нужен только **Docker** (Docker Desktop на Windows/macOS, docker + compose plugin на Linux).

```bash
git clone https://github.com/RuMemory/maxgraph
cd maxgraph
cp .env.example .env
```

Теперь сгенерируй `MEMORY_TOKEN` — ключ от памяти — и впиши его в `.env`:

```bash
# Linux / macOS
openssl rand -hex 24
```

```powershell
# Windows (PowerShell) — одна строка
-join ((1..48)|%{ '{0:x}' -f (Get-Random -Max 16) })
```

И сразу — `HOST_FP`: «отпечаток» твоего компьютера (хеш железа, само железо никуда не отправляется). Он делает триал и лицензию стабильными при пересоздании контейнера. Одна команда — и впиши результат (64 символа) строкой `HOST_FP=…` в `.env`:

```bash
# Linux
tr -d '[:space:]' < /etc/machine-id | sha256sum | awk '{print $1}'

# macOS
printf %s "$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}')" | shasum -a 256 | awk '{print $1}'
```

```powershell
# Windows (PowerShell)
$guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
[BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-','').ToLower()
```

При переустановке на том же компьютере `HOST_FP` не меняется — если строка уже есть в `.env`, оставляй её как есть.

Если ставил через установщик (`install.sh` / `install.ps1`), он дополнительно создаёт `hostfp.json` рядом с `docker-compose.yml` — тот же отпечаток компьютера, только хеши. Ничего настраивать не нужно; файла нет → дашборд покажет спокойный баннер с инструкцией (см. «[Unanchored-баннер](#unanchored)»).

Запуск:

```bash
docker compose pull   # готовый образ; нет сети до ghcr — пропусти, соберётся локально
docker compose up -d
```

Проверка, что всё живо:

```bash
curl http://localhost:8787/healthz
# → ok
```

---

## 3. Ввод license key

1. Открой страницу лицензии своей локальной установки: **http://localhost:8787/app**
2. Вставь **license key `mg_…`** из письма и нажми «Активировать».

То же самое из терминала, если любишь curl:

```bash
curl -X POST http://localhost:8787/api/license/activate \
  -H "Content-Type: application/json" \
  -d '{"key":"mg_…твой-ключ…"}'
```

**Что происходит за кулисами:**

```
Твой сервер ──(один раз, онлайн)──▶ POST rezon-ai.ru/api/license/validate
           ◀── ключ валиден ──────┘
           │
           ▼  пишет data/license.json
           │
           ▼  дальше — полный офлайн
              проверка HMAC-подписи локально, без интернета
```

Интернет нужен **ровно один раз** — на эту валидацию. Дальше сервер сам проверяет подпись ключа локально. Ставишь `LICENSED`-пилюлю — и работаешь хоть в бункере.

---

## 4. Смотрим память

### 4.1 Дашборд

**http://localhost:8787/app** — откроется форма входа: введи `<MEMORY_TOKEN>`, токен сохранится в браузере.

| Вкладка | Что там |
|---------|---------|
| **Граф** | Живой космос знаний: обновление каждые 8 секунд, новые сущности вспыхивают. Клик по звезде — контекст сбоку |
| **Таймлайн** | Все мутации графа: что агент запомнил и когда |
| **Сессии** | Карточки сессий — итоги разговоров |
| **⬇ export** | Полный JSON-бэкап одной кнопкой. Всегда, даже после триала |

### 4.2 REST API (тот же `MEMORY_TOKEN`)

```bash
T=<MEMORY_TOKEN>

# весь граф
curl -H "Authorization: Bearer $T" http://localhost:8787/api/graph

# журнал мутаций
curl -H "Authorization: Bearer $T" http://localhost:8787/api/timeline

# поиск по памяти
curl -H "Authorization: Bearer $T" "http://localhost:8787/api/search?q=deploy"

# бэкап в файл
curl -H "Authorization: Bearer $T" http://localhost:8787/api/export > backup.json
```

### 4.3 Подключение агента по MCP

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maxgraph": {
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

**DeepSeek Harness** — yaml:

```yaml
- id: mcp-remotemem
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: remotemem
    transport: streamable-http
    url: http://localhost:8787/mcp
    headers:
      Authorization: Bearer <MEMORY_TOKEN>
```

**Cursor** — файл `~/.cursor/mcp.json` (глобально) или `.cursor/mcp.json` в папке проекта:

```json
{
  "mcpServers": {
    "maxgraph": {
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

**Cline** (VS Code) — панель Cline → иконка MCP Servers → Configure → Configure MCP Servers,
или напрямую файл `~/.cline/data/settings/cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "maxgraph": {
      "type": "streamableHttp",
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

Поле `"type": "streamableHttp"` обязательно — без него Cline молча уходит в legacy-режим, с которым MaxGraph не работает.

**Windsurf / Devin Desktop** — файл `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "maxgraph": {
      "serverUrl": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

**ChatGPT** — два пути, обоим нужен публичный HTTPS до твоего инстанса (проще всего — Tailscale Funnel): свой GPT с действиями (пошагово — [GPTS.md](GPTS.md)) или chatgpt.com → Plugins → «Add custom MCP server» с тем же HTTPS-URL.

`<MEMORY_TOKEN>` — токен из шага 2 (лежит и в `data/local_token.txt` после первого запуска). После записи конфига — полностью перезапусти хост (не закрыть окно, а выйти) и начни новую сессию.

Перезапусти клиент — у агента появятся инструменты памяти: `create_entities`, `add_observations`, `search_nodes`, `read_graph` и другие (связи, обновления, таймлайн).

---

## 5. Troubleshooting

### «Сервер недоступен при активации»

Активации license key нужен интернет — сервер один раз сверяет ключ с магазином (`POST /api/license/validate`). Проверь, что машина онлайн, и повтори. После записи `data/license.json` интернет больше не нужен вообще.

### «Ключ не подошёл»

По порядку:

1. Ты вставил **license key `mg_…`**, а не код `MG-…`? Код сначала редимится на сайте и возвращает ключ — для сервера нужен именно ключ.
2. Ключ вставлен целиком, без пробелов и переносов строк? Это длинная строка вида `mg_<…>.<…>` — копируй прямо из письма.
3. Ключ привязан к email покупки и тарифу — чужой или старый ключ не сработает.
4. Всё равно не катит → поддержка через магазин, приложи email покупки. Разберёмся.

### «Лимит: ключ уже привязан к другому компьютеру»

Один license key = одна машина. Новый компьютер или пересборка — напиши на help@rezon-ai.ru с email покупки: отвяжем старую машину, ключ активируется.

<a id="unanchored"></a>
### «Установка не привязана к этому компьютеру»

Янтарный баннер в дашборде означает: сервер запущен без отпечатка компьютера (например, `docker compose up` вручную, в обход установщика). Всё работает, но учёт триала и машин будет неточным.

Лечение без переустановки — досчитать отпечаток (появится `hostfp.json` рядом с `docker-compose.yml`) и перезапустить контейнер:

```bash
# Linux / macOS
bash install.sh --hostfp-only && docker compose up -d
```

```powershell
# Windows (PowerShell)
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -HostfpOnly; docker compose up -d
```

Отпечаток — это хеш: сырые данные о компьютере никуда не отправляются.

### «Дашборд пуст»

Это нормально: память появляется, когда агент **подключён и пишет**. Пустой космос — это будущий граф. Сделай два шага:

1. Подключи MCP-конфиг (шаг 4.3) и перезапусти клиента.
2. Вставь в system-prompt / инструкции агента готовый блок протокола:

```
У тебя есть постоянная память Maxgraph (инструменты maxgraph/*).
В начале сессии вызови read_graph. Сохранять факты СРАЗУ через
add_observations; новые темы — create_entities (name = kebab-case id,
title = человеческое имя); зависимости — create_relations в активном залоге.
Полный протокол: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```

Начни разговор — и смотри, как на графе вспыхивают первые звёзды.

### «Инструменты maxgraph не появились в агенте»

Сервер жив, а агент его «не видит» (в ответах нет вызовов памяти)? Это почти всегда порядок запуска: хост подхватывает MCP-подключение и правила из `AGENTS.md` **при старте сессии**. Если сессия стартовала раньше сервера — или конфиг записали уже после старта — инструментов в ней не будет.

Лечение:

1. Полностью выйди из хоста (Claude Desktop / Cursor / DSH — «Выход» из трея, не крестик окна).
2. Запусти хост снова и начни **новую** сессию в папке проекта.
3. Проверка: напиши «запомни: я только что установил MaxGraph» — в ответе появятся вызовы инструментов памяти, а на карте дашборда — первая точка.

Не помогло → проверь, что конфиг из шага 4.3 на месте и сервер отвечает (`curl http://localhost:8787/healthz` → `ok`), дальше — поддержка через магазин.

### «Как перенести память на другой компьютер»

Формат экспорта — NDJSON, переносится за минуту:

1. На старой машине: кнопка **⬇ export** в дашборде (или `curl … /api/export > backup.json`).
2. На новой: подними свежую установку (шаг 2), останови контейнер.
3. Из бэкапа распакуй: `memory_ndjson` → `data/memory.json`, `events_jsonl` → `data/events.jsonl`. С v0.34.0 (шарды): если `data/projects/` существует — удали его до запуска (миграция сама разложит монолит по шардам).
4. Запусти контейнер. Тот же license key вводишь в `/app` новой установки. Один ключ живёт на двух машинах одновременно; для третьей напиши на help@rezon-ai.ru с email покупки — отвяжем лишнюю.

---

## 6. Безопасность

Коротко, но важно:

- **Не публикуй** license key `mg_…` и `MEMORY_TOKEN`. Каждый из них — полный доступ к твоей памяти. Скриншоты дашборда — без адресной строки с токеном.
- **`MEMORY_TOKEN` живёт в `.env`**, а не в git. Репозиторий публичный — токен нет.
- **Read-only токены** (тариф Team) — для демо и коллег: смотреть можно, менять нельзя.
- **Бэкапься.** Кнопка ⬇ export — одна секунда. Ежедневные автобэкапы уже включены в Pro/Team, но свой JSON на диске лишним не бывает.
- Потерял license key → пиши в поддержку с email покупки, восстановим.

---

Готово: ключ активирован, сервер лицензирован, агент пишет в граф.
Дальше: [QUICKSTART](QUICKSTART.md) · [PROVIDERS](PROVIDERS.md) · [PROTOCOL](PROTOCOL.md)
