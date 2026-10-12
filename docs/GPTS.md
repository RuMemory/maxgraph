# 🤖 Maxgraph + ChatGPT (GPTs Actions)

Как дать своему GPT доступ к памяти Maxgraph через [openapi.yaml](openapi.yaml).

## 1. Сделай инстанс доступным по HTTPS

GPTs Actions работают только с публичным HTTPS. Варианты:

- **Tailscale Funnel** (рекомендуем, инстанс остаётся «домашним»):
  ```bash
  tailscale funnel --bg 8787
  # → https://<machine>.<tailnet>.ts.net
  ```
- Обратный прокси с TLS (nginx/caddy) на своём домене.

Проверь извне: `curl https://<твой-https>/healthz` → `ok`.

## 2. Подготовь схему

В `docs/openapi.yaml` замени `https://memory.example.com` на свой URL из шага 1
(строчка `servers:` — первая).

## 3. Создай GPT

1. [chatgpt.com](https://chatgpt.com) → **Explore GPTs → Create → Configure**.
2. В инструкции GPT вставь блок из [maxgraph-agent.md](../maxgraph-agent.md).
3. **Actions → Create new action**:
   - **Schema**: вставь содержимое `docs/openapi.yaml`.
   - **Authentication**: `API Key` → `Bearer` → значение = твой `MEMORY_TOKEN`.
   - **Privacy policy**: ссылка на свой репо/сайт (любая валидная ссылка).
4. Сохрани.

## 4. Проверь

В чате с GPT: «запомни, что мой любимый стек — Python + FastAPI» → должен вызвать
`createEntities` + `addObservations`. Затем «что ты обо мне помнишь?» → `searchMemory`
или `readGraph`. В дашборде (`/app`) появятся новые звёзды.

## Замечания

- GPT ходит в твой инстанс напрямую — токен знает только OpenAI-хранилище action'а
  и твой сервер. Токен можно ротировать (`.env` + перезапуск), не трогая граф.
- Записывающие операции (`createEntities` и т.д.) возвращают `402`, если триал истёк
  и лицензия не активирована — GPT честно скажет об этом текстом из `error`.
- Лимит схемы GPTs: до 30 операций — мы используем 11 из них.
