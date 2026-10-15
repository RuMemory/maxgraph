# 🧠 Инструкции памяти Maxgraph для твоего агента

Готовый блок для копипаста в system-prompt, `AGENTS.md` или `CLAUDE.md` **любого** агента, подключённого к Maxgraph по MCP. Он учит агента вести память по протоколу: читать граф на старте, писать факты сразу, фиксировать итог сессии.

В `AGENTS.md` проекта блок вставляется в КОРНЕ рабочей папки проекта между маркерами `<!-- maxgraph:start -->` и `<!-- maxgraph:end -->`; повторная установка заменяет только содержимое между маркерами. Не в подпапку установки и не в папки хоста.

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

УДАЛЕНИЕ MAXGRAPH по просьбе пользователя — полный порядок, без хвостов:
1) в папке установки запусти uninstall.sh (macOS/Linux) / uninstall.ps1 (Windows) —
   снимает контейнеры/образы и служебные остатки; данные памяти (./data) по
   умолчанию СОХРАНЯЮТСЯ, полное стирание — только после явного «да» пользователя
   или с флагом --wipe-data;
2) удали запись "maxgraph" из конфигов MCP-клиентов;
3) удали этот блок (между <!-- maxgraph:start --> и <!-- maxgraph:end -->) из
   AGENTS.md; в ГЛОБАЛЬНЫХ правилах агента — покажи пользователю найденные строки
   про maxgraph и удаляй только установочные блоки после его явного «да»;
4) если данные сохранялись — ПЕРЕНЕСИ ./data из папки установки до её удаления
   (или удали всё, кроме data/): удаление папки не должно унести память.
   Папку удаляй после подтверждения пользователя.
Папка установки уже удалена? Тогда: docker rm -f maxgraph maxgraph-updater,
потом шаги 2–4 вручную.

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

UNINSTALL on the user's request — full cleanup, no leftovers:
1) run uninstall.sh (macOS/Linux) / uninstall.ps1 (Windows) in the install folder —
   removes containers/images and leftovers; memory data (./data) is KEPT by default,
   wipe only after the user's explicit "yes" or with --wipe-data;
2) remove the "maxgraph" entry from MCP client configs;
3) remove this block (between <!-- maxgraph:start --> and <!-- maxgraph:end -->)
   from AGENTS.md; in the agent's GLOBAL rules — show the user any maxgraph
   lines found and remove only install-made blocks after their explicit "yes";
4) if data was kept — MOVE ./data out of the install folder BEFORE deleting it
   (or delete everything except data/): removing the folder must not take the
   memory with it. Delete the folder after the user's confirmation.
Install folder already gone? Then: docker rm -f maxgraph maxgraph-updater,
then steps 2–4 by hand.

Full protocol: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```
