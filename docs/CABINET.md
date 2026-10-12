# 🏠 Cabinet — личный кабинет покупателя (дизайн v0.4+)

Каждый покупатель получает **уникальный кабинет** со своими параметрами и контекстами всех своих моделей.

## Модель

```
maxgraph.cloud
 └── tenant: nikita                    ← личный кабинет (namespace)
      ├── shared/                      ← общий граф (якоря, профиль)
      ├── profiles/
      │    ├── claude/                 ← контекст Claude (архитектура, код)
      │    ├── gpt/                    ← контекст GPT (тексты, идеи)
      │    ├── kimi/                   ← контекст Kimi (DSH-окружение)
      │    ├── glm/                    ← контекст GLM
      │    └── hermes/                 ← контекст Hermes
      ├── tokens:  maxgraph_xxx (rw), maxgraph_ro_xxx (read-only)
      └── license: PRO, до 2027-10-05
```

## Реализация (пошагово)

1. **Namespaces в ядре**: префикс `data/tenants/<id>/memory.json`; авторизация: токен → tenant
2. **Профили провайдеров**: сущности с `tags:["profile:claude"]` либо физические под-файлы; MCP-инструмент `use_profile(name)` переключает слой; `shared/` читается всегда
3. **Кабинет-дашборд**: переключатель профиля в шапке, счётчики по профилям, квоты тарифа
4. **License key**: HMAC-подпись `{tenant, tier, exp}`; self-hosted pro-функции проверяют ключ офлайн; облако — по подписке

## Апгрейд с Free на Pro без потерь

Экспорт Free (`/api/export`) → импорт в кабинет Pro. Формат один — NDJSON.

## Безопасность

- Tenant isolation на уровне файлов + middleware (токен знает свой namespace)
- Read-only токены для демо/команды
- rate limit per-token (v0.4)
