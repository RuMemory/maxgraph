# embed-relay (maxgraph)

Hosted embedding-сервис для self-hosted инстансов maxgraph. Живёт на VPS rezon-ai.ru
отдельным контейнером рядом с магазином; nginx проксирует
`https://rezon-ai.ru/observer/embeddings` → `POST /embeddings` этого сервиса.

Модель: `intfloat/multilingual-e5-small` (384 dims), инференс локальный (fastembed/onnxruntime).
Ответ — OpenAI-compatible: `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]}],"model":...,"usage":{...}}`,
`prompt_tokens = total_tokens` — оценка: ASCII `chars // 4`, не-ASCII (CJK/emoji) ≈ `chars` (защита квоты от недосчёта ~4×).

## Env

| Переменная | Обязательна | Назначение |
|---|---|---|
| `RELAY_HMAC_SECRET` | да | HMAC-ключ проверки relay-токенов; без него сервис не стартует |
| `EMBED_DATA_DIR` | нет (default `./data`) | каталог для `usage.json` (учёт квоты) |

## Docker

```bash
docker build -t maxgraph-embed services/embed
docker run -d --name maxgraph-embed --restart unless-stopped \
  -e RELAY_HMAC_SECRET=<секрет> \
  -v embed-usage:/app/data \
  -p 127.0.0.1:8788:8788 \
  maxgraph-embed
```

Модель (~500MB) скачивается при сборке образа, контейнеру сеть для старта не нужна.
После скачивания sha256 ONNX сверяется с эталоном ревизии `614241f6` (несовпадение =
сборка падает). Volume `/app/data` — чтобы квота переживала перезапуск.

nginx (путь ВАЖЕН: `proxy_pass …/embeddings;` — без URI уйдёт исходный `/observer/embeddings`
и FastAPI ответит 404; каноничный блок ставит `deploy/deploy-embed.sh`):
`location = /observer/embeddings { proxy_pass http://127.0.0.1:8788/embeddings; client_max_body_size 160k; }`

ВАЖНО: запуск только в 1 процесс (`uvicorn … ` без `--workers` — так в Dockerfile CMD):
квота `usage.json` процессно-локальна, workers>1 даст каждому воркеру свою квоту.

## Проверка

Генерация тестового токена (lk и exp подставить свои):

```bash
TOKEN=$(python -c "import base64,hashlib,hmac,json,time;p=base64.urlsafe_b64encode(json.dumps({'lk':'<lk16>','exp':int(time.time())+86400},separators=(',',':')).encode()).decode().rstrip('=');print(p+'.'+hmac.new(b'<секрет>',('relay:'+p).encode(),hashlib.sha256).hexdigest())")

curl -X POST http://127.0.0.1:8788/embeddings \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"input":"привет","model":"intfloat/multilingual-e5-small"}'
```

## Формат relay-токена (спека для магазина, который их выпускает)

```
token = b64url(payload_json) + "." + hex(HMAC_SHA256(RELAY_HMAC_SECRET, "relay:" + b64url(payload_json)))
```

- `b64url` — base64url без padding (`=`); `hex` — lowercase hex (64 символа), как `licensing.relay_token` на магазине.
- Подписывается строка `"relay:" + <payload_b64>` (с префиксом, по ПРИНЯТОМУ сегменту — JSON не пересериализуем).
- `payload_json = {"lk": "<первые 16 символов hex sha256 лицензионного ключа>", "exp": <unix epoch>}` — compact separators (`,`,`:`), порядок ключей lk,exp.
- Сервис отклоняет: нет токена / плохая подпись / истёкший `exp` → `401 {"error":"unauthorized"}`.

## Лимиты и квота

- Тело запроса ≤ 128 KB → иначе `413`.
- `input`: строка или список ≤ 64 текстов, каждый ≤ 8000 символов → иначе `400`.
- Квота: 200 000 токенов (chars//4) на `lk` в UTC-день → превышение `429 {"error":"quota_exceeded","limit":200000}`.
- Учёт квоты: `data/usage.json`, запись атомарная (tmp + os.replace).

## Privacy

**Контент текстов никогда не логируется** (требование владельца). Логируется только
usage-строка: `ts, lk, tokens, batch_size`.

## Тесты

```bash
pip install fastapi httpx          # fastembed не нужен, инференс замокан
python -m unittest test_main -v
SMOKE_E2E=1 python -m unittest test_main -v   # + реальный инференс (скачает модель)
```
