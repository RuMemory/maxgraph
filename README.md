![MaxGraph — Your AI forgets. MaxGraph remembers.](docs/assets/banner.jpg)

# 🌌 MaxGraph

🇬🇧 [English version](README_EN.md) · Русский

## Хватит объяснять нейросети всё сначала

MaxGraph — память для работы с нейросетью. Он запоминает твой проект: что решили, что сломали, что пообещали. В следующей сессии агент сам это находит и начинает там, где ты закончил.

Работает с Claude, ChatGPT, Cursor, Kimi и DeepSeek. Пользуешься другим агентом? Если он умеет подключать внешние инструменты — MaxGraph к нему подключится, детали в таблице ниже.

🌐 Сайт и покупка: [rezon-ai.ru](https://rezon-ai.ru) · Триал 7 дней, без карты · Экономия ~43 % токенов

![license](https://img.shields.io/badge/license-BSL--1.1-orange) ![docker](https://img.shields.io/badge/docker-ready-38d39f) ![MCP](https://img.shields.io/badge/MCP-streamable--http-ff5bd8)

![Дашборд MaxGraph: карта памяти проекта, свежие воспоминания и лента записей](docs/assets/demo-dashboard.png)

## Что ты получаешь

- Агент помнит проект между сессиями: решения, баги, задачи и договорённости — с датами.
- Записи лежат отдельно от чата. Диалог можно закрыть или удалить, записи это не заденет.
- Каждая новая сессия начинается с короткой выжимки по проекту. Перечитывать всю историю не нужно — отсюда и экономия токенов.
- Незаконченные дела всплывают сами, напоминать о них не нужно.
- Всё записанное видно глазами в дашборде: карта памяти, поиск, лента событий.

## Установка (2 минуты)

Нужен только Docker:

```bash
git clone https://github.com/RuMemory/maxgraph && cd maxgraph
docker compose up -d --build
```

Токен доступа генерируется сам при первом запуске — возьми его из логов:

```bash
docker compose logs maxgraph | grep "автогенерация"   # или: cat data/local_token.txt
```

Открой дашборд `http://localhost:8787/app` и введи токен. Подключение твоего агента: [docs/QUICKSTART.md](docs/QUICKSTART.md).

Обновления ставятся сами, раз в сутки. Не нужны — отключи: `AUTO_UPDATE=0`.

## С кем работает

| Клиент | Как подключить |
|---|---|
| Claude Desktop / Claude Code | MCP config → `http://host:8787/mcp` |
| Cursor / Cline / Continue | MCP streamable-http + Bearer |
| DeepSeek Harness | `dsh-mcp-client`, см. [PROVIDERS](docs/PROVIDERS.md) |
| ChatGPT / OpenAI-совместимые | REST API или агент-адаптер |
| Kimi, GLM, Qwen, Hermes… | REST API — любой HTTP-клиент |

Готовые примеры настройки под каждый клиент: [docs/PROVIDERS.md](docs/PROVIDERS.md)

## Цены

**7 дней бесплатно, без карты** — триал начинается с первого запуска, включать ничего не нужно. Дальше один тариф со всем включённым, разовый платёж без автосписаний:

| Срок | Цена | В месяц |
|---|---|---|
| 1 месяц | 149 ₽ | 149 ₽ |
| 3 месяца | 399 ₽ | ≈ 133 ₽ |
| 6 месяцев | 749 ₽ | ≈ 125 ₽ |
| 12 месяцев | 1 299 ₽ | ≈ 108 ₽ |

Когда триал закончится, перестанет работать только запись новых фактов. Дашборд, поиск и выгрузка остаются — накопленная память никуда не денется.

Купить и активировать: [rezon-ai.ru](https://rezon-ai.ru)

## 💛 Поддержать

MaxGraph живёт на поддержку пользователей. Если проект оказался полезным — можно просто сказать спасибо: [rezon-ai.ru#donate](https://rezon-ai.ru/#donate). Разовый перевод, без подписки и автосписаний.

## Приватность

Все записи хранятся только на твоём компьютере. Это обычные текстовые файлы в папке `data/`: их можно открыть, прочитать и скопировать в любой момент.

## Лицензия

[BSL-1.1](LICENSE) — исходный код доступен: читай, меняй и запускай у себя свободно, личное и внутреннее использование бесплатно. Продажа MaxGraph как сервиса — по коммерческой лицензии. Через четыре года после публикации каждая версия становится MIT.

## Docs

[QUICKSTART](docs/QUICKSTART.md) · [PROTOCOL](docs/PROTOCOL.md) · [API](docs/API.md) · [GPTs](docs/GPTS.md) · [PROVIDERS](docs/PROVIDERS.md) · [CABINET](docs/CABINET.md) · [DEPLOY-LAN](docs/DEPLOY-LAN.md)

## Keywords

`mcp server` `ai memory` `llm context` `knowledge graph` `claude memory` `persistent memory for ai agents` `model context protocol` `self-hosted ai` `agent memory` `context engineering` `semantic search` `long-term memory for llm` `память для нейросети` `память для ИИ-агентов` `MCP сервер памяти` `граф знаний` `долговременная память ИИ` `экономия токенов контекста` `память для Claude` `память для ChatGPT` `память для DeepSeek` `локальная память для ИИ`
