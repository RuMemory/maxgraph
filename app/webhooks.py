#!/usr/bin/env python3
"""Digiseller «Уведомления» (sale/message) → лог + Telegram.

Чистый stdlib, тестируется отдельно от сервера.

Digiseller шлёт (см. my.digiseller.com → Личный кабинет → Уведомления → URL):
- Продажа (POST JSON): {"ID_I","ID_D","Amount","Currency","Email","Date","SHA256",...}
- Сообщение (POST JSON): {"DebateId","MessageId","OwnerId","MessageDate","Message","InvoiceId","ImagePath"}
GET-вариант — те же поля в query (snake_case). Принимаем оба: app.py складывает в плоский dict.

Авторизация — секрет в URL (?key=...), см. app.py. SHA256 Digiseller не проверяем:
он считается от пароля аккаунта, которого у сервера нет (и не должно быть).
"""

import json
import time


def normalize(params: dict) -> dict:
    """Плоский dict с любыми ключами → каноничный snake_case."""
    p = {str(k).lower(): ("" if v is None else str(v)) for k, v in params.items()}

    def pick(*names: str) -> str:
        for n in names:
            if p.get(n):
                return p[n]
        return ""

    return {
        "order": pick("id_i", "invoiceid", "order_id"),
        "product": pick("id_d", "product_id", "id_goods"),
        "amount": pick("amount"),
        "currency": pick("curr", "currency"),
        "email": pick("email"),
        "date": pick("date", "messagedate"),
        "message": pick("message", "messagetext"),
        "debate_id": pick("debateid"),
        "image": pick("imagepath"),
        "cart_uid": pick("cartuid"),
        "is_my_product": pick("ismyproduct"),
    }


def format_telegram(kind: str, d: dict) -> str:
    """Текст уведомления для Telegram. kind ∈ {sale, message}."""
    if kind == "sale":
        lines = ["🛒 Digiseller: продажа"]
        if d["product"]:
            lines.append(f"Товар: {d['product']}")
        if d["amount"]:
            lines.append(f"Сумма: {d['amount']} {d['currency']}".rstrip())
        if d["email"]:
            lines.append(f"Покупатель: {d['email']}")
        if d["order"]:
            lines.append(f"Заказ: {d['order']}")
        return "\n".join(lines)
    lines = ["💬 Digiseller: сообщение"]
    if d["order"]:
        lines.append(f"Заказ: {d['order']}")
    if d["message"]:
        lines.append(d["message"][:900])
    if d["image"]:
        lines.append(f"🖼 {d['image']}")
    return "\n".join(lines)


def append_log(path, kind: str, d: dict, raw: dict) -> None:
    """Аудит в JSONL (одна строка = одно событие)."""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind,
           "data": d, "raw": {str(k): str(v)[:500] for k, v in raw.items()}}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
