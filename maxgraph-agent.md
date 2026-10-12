# 🧠 Инструкции памяти Maxgraph для твоего агента

Готовый блок для копипаста в system-prompt, `AGENTS.md` или `CLAUDE.md` **любого** агента, подключённого к Maxgraph по MCP. Он учит агента вести память по протоколу: читать граф на старте, писать факты сразу, фиксировать итог сессии.

Подключение агента к серверу — шаг 3 в [Quickstart](docs/QUICKSTART.md).

## Блок (RU)

```text
У тебя есть постоянная память Maxgraph (инструменты maxgraph/*).

СТАРТ СЕССИИ: вызови read_graph — загрузи граф знаний. Нужна конкретная тема —
search_nodes по запросу.

ВО ВРЕМЯ РАБОТЫ — ПИШИ СРАЗУ, не откладывай на конец разговора:
- новый проект/сервис/файл/инструмент → create_entities (с title, entityType, tags);
  name = kebab-case id навсегда, title = человеческое имя;
  перед созданием — search_nodes, существующие не дублировать;
- новый durable-факт о существующем → add_observations
  (одно наблюдение = один атомарный факт, со ссылкой на дату/версию, если важно);
- объект X использует/зависит от Y → create_relations в активном залоге
  ("uses", "depends on", "configured by", "hosted on", "part of" — не "is used by");
- изменился статус/параметр → add_observations (новый факт с датой),
  при смене имени — update_entity.

КОНЕЦ СЕССИИ: создай сущность session-YYYY-MM-DD с итогами разговора
(entityType: "session") и свяжи её связью "discussed" с затронутыми сущностями.

Полный протокол: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```

## Блок (EN, короткий)

```text
You have persistent memory via Maxgraph (maxgraph/* tools).

Session start: call read_graph; use search_nodes for a specific topic.
While working — WRITE IMMEDIATELY, never batch to the end:
new topic → create_entities (name = kebab-case id, title = human name; search first, don't duplicate);
new durable fact about an existing entity → add_observations (one observation = one atomic fact, dated when it matters);
dependencies → create_relations in active voice ("uses", "depends on", "configured by").
Session end: create a session-YYYY-MM-DD entity with the outcomes, linked with "discussed"
to every entity the conversation touched.

Full protocol: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```
