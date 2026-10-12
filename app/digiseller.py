#!/usr/bin/env python3
"""
Maxgraph × Digiseller — генерация кода по заказу («данные, полученные от вашего сервера»).

Чистый stdlib (hashlib/hmac/json) — модуль тестируется отдельно от сервера
(без mcp/starlette; стиль как billing.py — зависимости инъектируются).

Контракт Digiseller (исследование 2026-10-06; подробности — во внутренних заметках владельца, вне репо):
  ПОДТВЕРЖДЕНО официальными источниками:
  - Товар «Произвольный цифровой товар» с content_type="form": после оплаты
    Digiseller отправляет HTTP-запрос (POST) на URL продавца с реквизитами
    платежа; флаг «наличие товара в ответе сервера» (answer=true) означает,
    что ответ сервера содержит товар, который видит покупатель.
    (ru.webmoney.wiki/projects/digiseller/wiki → «…товар через стороннее API»)
  - Настройка через API: POST /api/product/content/update/form
    {product_id, address, method: Email|XML|JSON, encoding, options, answer,
     allow_purchase_multiple_items, url_for_quantity}
    (my.digiseller.com/inside/api_content.asp#update_form;
     swagger api.digiseller.com/swagger/docs/v1 → ChangeProductFormRequest)
  - create/arbitrary поддерживает content_type: text|file|form; uniquefixed —
    только text|digisellercode|file → текущий товар XXXXXXXX нельзя переключить
    редактированием, нужен НОВЫЙ товар типа arbitrary.
  ПРЕДПОЛОЖЕНИЯ (публично не задокументированы — всё конфигурируется env):
  - Точные имена полей запроса: принимаем синонимы (id_cart/order_id/inv/…).
  - Схема подписи: DIGISELLER_SIGN_SCHEME. Default "url-key" — секрет в самом
    URL генерации (?key=…): надёжно и не зависит от того, как Digiseller
    считает свой sign (md5-подписи упоминаются в старых гайдах, схема не
    подтверждена официально).
  - Формат ответа: DIGISELLER_RESPONSE_FORMAT (default "text" — сырой код
    в теле; fail-safe: даже если Digiseller не разберёт обёртку, покупатель
    увидит сам код; при method=XML/JSON в кабинете переключить на xml/json).

Env (читает app.py, модуль берёт значения параметрами):
  DIGISELLER_GEN_SECRET      — shared secret эндпоинта (НЕ API key Digiseller)
  DIGISELLER_SIGN_SCHEME     — url-key | md5 | md5-full | sha256-full | hmac-sha256
                               (можно списком через запятую — примет первую подошедшую)
  DIGISELLER_RESPONSE_FORMAT — text | json | xml
"""

import hashlib
import hmac
import json
from xml.sax.saxutils import escape as _xml_escape

# --- синонимы имён параметров (Digiseller публично не документирует набор) ---
ORDER_KEYS = ("id_cart", "order_id", "order", "inv", "invoice_id", "invoice", "order_num")
EMAIL_KEYS = ("email", "payer_email", "buyer_email", "mail")
AMOUNT_KEYS = ("amount", "sum", "total", "price", "amount_total")
CURRENCY_KEYS = ("currency", "curr", "type_curr", "currency_type")
PRODUCT_KEYS = ("product_id", "id_goods", "id_good", "item_id", "goods_id")
SIGN_KEYS = ("sign", "signature", "hash")
KEY_KEYS = ("key", "token")  # секрет в URL (схема url-key)

_SIGN_AND_KEY = frozenset(SIGN_KEYS) | frozenset(KEY_KEYS)
FORMATS = ("text", "json", "xml")


class DigisellerError(Exception):
    """Fail-loud ошибка входящего запроса Digiseller (плохие параметры)."""


# ---------------------------------------------------------------- params ----

def _first(params: dict, keys, limit: int = 512) -> str:
    """Первое непустое строковое значение по списку имён (регистр не важен)."""
    low = {str(k).lower(): v for k, v in (params or {}).items()}
    for k in keys:
        v = low.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v[:limit]
    return ""


def order_id_of(params: dict) -> str:
    return _first(params, ORDER_KEYS, limit=64)


def email_of(params: dict):
    """email покупателя → lower или None (невалидный не мешает выдаче)."""
    email = _first(params, EMAIL_KEYS, limit=254).lower()
    if email and "@" in email and " " not in email:
        return email
    return None


def amount_of(params: dict):
    """Сумма → float | None. Принимаем и «390.00», и «390,00»."""
    raw = _first(params, AMOUNT_KEYS, limit=32).replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def currency_of(params: dict) -> str:
    return _first(params, CURRENCY_KEYS, limit=16).upper()


def product_id_of(params: dict) -> str:
    return _first(params, PRODUCT_KEYS, limit=32)


# ------------------------------------------------------------- signature ----

def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _hmac_hex(secret: str, s: str) -> str:
    return hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(params: dict, secret: str, scheme: str = "url-key") -> bool:
    """Проверить запрос. scheme — одна или список через запятую; пустой секрет → False.

    Схемы:
      url-key       — параметр key/token равен секрету (секрет в URL кабинета); DEFAULT
      md5           — sign = md5(secret + order_id)
      md5-full      — sign = md5(secret + order_id + amount + currency)
      sha256-full   — sign = sha256(secret + order_id + amount + currency)
      hmac-sha256   — sign = HMAC_SHA256(secret, "k=v&…" sorted, без sign/key)
    """
    if not secret:
        return False
    schemes = [s.strip().lower() for s in str(scheme or "url-key").split(",") if s.strip()]
    return any(_verify_one(params or {}, secret, s) for s in schemes)


def _verify_one(params: dict, secret: str, scheme: str) -> bool:
    if scheme == "url-key":
        given = _first(params, KEY_KEYS, limit=256)
        return bool(given) and hmac.compare_digest(given, secret)
    sign = _first(params, SIGN_KEYS, limit=256).lower()
    if not sign:
        return False
    order = order_id_of(params)
    if scheme == "md5":
        return hmac.compare_digest(sign, _md5_hex(secret + order))
    if scheme == "md5-full":
        return hmac.compare_digest(sign, _md5_hex(secret + order + _first(params, AMOUNT_KEYS)
                                                + currency_of(params)))
    if scheme == "sha256-full":
        return hmac.compare_digest(sign, _sha256_hex(secret + order + _first(params, AMOUNT_KEYS)
                                                     + currency_of(params)))
    if scheme == "hmac-sha256":
        canon = "&".join(f"{k}={v}" for k, v in sorted(params.items(), key=lambda kv: str(kv[0]))
                         if str(k).lower() not in _SIGN_AND_KEY)
        return hmac.compare_digest(sign, _hmac_hex(secret, canon))
    return False


# -------------------------------------------------------------- response ----

def _cdata(s: str) -> str:
    """Безопасная упаковка в CDATA (разрыв ']]>')."""
    return s.replace("]]>", "]]]]><![CDATA[>")


def build_response(code=None, error=None, fmt: str = "text", product_id="", inv=0):
    """Ответ Digiseller «Отправка заказа» (api_supplier.asp#order) → (body, content_type).

    json: {"id": "<id товара>", "inv": <номер>, "goods": код} | {"id","inv","error": "…"}
    xml:  <response><id/><inv/><goods><![CDATA[код]]></goods></response> (или <error/>)
    text: тело = сам код (fail-safe) | "error: …"
    id/inv эхом из запроса всегда; без goods И error Digiseller ретраит заказ до 50 раз,
    а HTTP-ошибка — тоже ретрай → бизнес-отказы отдаём 200 + error.
    """
    fmt = (fmt or "text").lower()
    try:
        inv_i = int(inv)
    except (TypeError, ValueError):
        inv_i = 0
    if fmt == "json":
        resp = {"id": str(product_id or ""), "inv": inv_i}
        resp["goods" if code else "error"] = str(code) if code else str(error or "error")
        return json.dumps(resp, ensure_ascii=False), "application/json; charset=utf-8"
    if fmt == "xml":
        inner = (f"<id>{_xml_escape(str(product_id or ''))}</id><inv>{inv_i}</inv>"
                 + (f"<goods><![CDATA[{_cdata(str(code))}]]></goods>" if code
                    else f"<error><![CDATA[{_cdata(str(error or 'error'))}]]></error>"))
        return f"<response>{inner}</response>", "text/xml; charset=utf-8"
    return (str(code) if code else f"error: {error or 'error'}"), "text/plain; charset=utf-8"


# ----------------------------------------------------------------- order ----

def handle_order(data: dict, params: dict, *, new_code, code_rec, gen_license_key, now_iso,
                 plan_for_amount=None, days_for_plan=None, sub_days: int = 31,
                 default_plan: str = "pro1", expected_product_id=None) -> dict:
    """Код по заказу, идемпотентно. Мутирует data (codes.json); сохранение — у вызывающего.

    data     — содержимое codes.json {"codes": [...], "payments": [...]} (под локом!)
    Инъекции: new_code() → "MG-…"; code_rec(code, tier, status, note) → dict;
              gen_license_key(email, tier, days) → "mg_…"; now_iso() → str;
              plan_for_amount(amount, currency) → plan id ("pro1"|"pro3"|…);
              days_for_plan(plan) → int (срок лицензии из плана; без неё — sub_days).
    → {"ok", "dedup", "order", "payId", "code", "licenseKey", "tier", "plan", "email"}
    Raises DigisellerError при отсутствии order id / чужом product id.
    """
    order = order_id_of(params)
    if not order:
        raise DigisellerError("order id required (id_cart)")
    pid = product_id_of(params)
    if expected_product_id and pid and pid != str(expected_product_id):
        raise DigisellerError(f"unknown product {pid} (ожидается {expected_product_id})")
    email = email_of(params)
    amount = amount_of(params)
    currency = currency_of(params)
    plan = default_plan
    if amount is not None and plan_for_amount is not None:
        plan = plan_for_amount(amount, currency)
    days = days_for_plan(plan) if days_for_plan else sub_days
    tier = "pro"  # v0.13.0: один тариф, длительность — из плана
    pay_id = f"dg-{order}"
    payments = data.setdefault("payments", [])
    codes = data.setdefault("codes", [])
    payment = next((p for p in payments if p.get("id") == pay_id), None)
    if payment and payment.get("code"):
        return {"ok": True, "dedup": True, "order": order, "payId": pay_id,
                "code": payment["code"], "licenseKey": payment.get("licenseKey"),
                "tier": payment.get("tier"), "plan": payment.get("plan"), "email": payment.get("email")}
    code = new_code()
    existing = {c.get("code") for c in codes}
    while code in existing:
        code = new_code()
    lic = gen_license_key(email or "buyer@unknown", tier, days)
    ts = now_iso()
    rec = payment or {}
    rec.update({"id": pay_id, "ref": pay_id, "tier": tier, "plan": plan, "status": "paid",
                "source": "digiseller", "code": code, "licenseKey": lic})
    rec.setdefault("at", ts)
    rec["email"] = email or rec.get("email")
    if amount is not None:
        rec["amount"] = amount
        rec["currency"] = currency
    if pid:
        rec["product_id"] = pid
    if payment is None:
        payments.append(rec)
    codes.append({**code_rec(code, tier, "sold", f"digiseller:{order}"),
                  "licenseKey": lic, "plan": plan})
    return {"ok": True, "dedup": False, "order": order, "payId": pay_id, "code": code,
            "licenseKey": lic, "tier": tier, "plan": plan, "email": rec.get("email")}
