#!/usr/bin/env python3
"""
Maxgraph billing — создание платежа в Platega + доставка кода покупателю.

Чистый stdlib (urllib, smtplib, json, hmac не нужен — auth по заголовкам) —
модуль тестируется отдельно от сервера (инъекция http_post/smtp_factory).

Platega API (проверено по https://docs.platega.io, 2026-10):
  POST {api_base}/v2/transaction/process
  headers: X-MerchantId, X-Secret
  body: {"paymentDetails": {"amount", "currency"}, "description", "return",
         "failedUrl", "payload", "metadata": {"userId"}}
  → 200 {"transactionId", "status", "url", "expiresIn", ...}
Callback о статусе НЕ несёт email плательщика — email сохраняем у себя
в codes.json payments при создании платежа (это делает app.py).

Доставка кода (deliver_code): email (SMTP+STARTTLS) и/или Telegram — каждый
канал best-effort: ошибка одного не роняет остальные, всё логируется.
Запись в codes.json и показ на success-странице — ответственность app.py
(webhook уже пишет payment+code до вызова deliver_code).

Env:
  PLATEGA_API_BASE  — base URL API (default https://app.platega.io)
  PLATEGA_CURRENCY  — валюта цен (default RUB)
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM — email-канал
  TG_BOT_TOKEN      — telegram-канал (chat id передаётся вызовом)
"""

import json
import logging
import os
import smtplib
import urllib.request
import html as _html
from email.message import EmailMessage

log = logging.getLogger("maxgraph.billing")

# официальный base URL из docs.platega.io — https://app.platega.io/
# (в техзадании фигурировал api.platega.io — это заглушка; рабочий — app.platega.io)
DEFAULT_API_BASE = "https://app.platega.io"
# v0.23.0: один тариф Pro, лесенка длительности (скидки от базы 149₽/мес: −11/−16/−27%)
PRICES = {"pro1": {"RUB": 149, "USD": 1.99}, "pro3": {"RUB": 399, "USD": 4.99},
          "pro6": {"RUB": 749, "USD": 9.99}, "pro12": {"RUB": 1299, "USD": 14.99}}
LABELS = {"pro1": "Maxgraph Pro — 1 месяц", "pro3": "Maxgraph Pro — 3 месяца",
          "pro6": "Maxgraph Pro — 6 месяцев", "pro12": "Maxgraph Pro — 12 месяцев"}


class BillingError(Exception):
    """Fail-loud ошибка биллинга (конфигурация/сеть/ответ Platega)."""


# ------------------------------------------------------------------ http ----

def _http_post_json(url: str, headers: dict, payload: dict, timeout: int = 20) -> dict:
    """POST json → dict. Точка инъекции для тестов — параметр http_post."""
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------- platega ----

def create_platega_payment(merchant_id: str, api_key: str, plan: str, email: str,
                           return_url: str, api_base: str = DEFAULT_API_BASE,
                           currency: str | None = None, failed_url: str | None = None,
                           payload: str = "", http_post=None) -> dict:
    """Создать платёж → {"payment_id", "payment_url"}.

    plan — id из PRICES ("pro1"|"pro3"|"pro6"|"pro12").
    Предположение по схеме ответа: берём transactionId/url, с запасом смотрим
    data.redirect/link/paymentUrl (формат менялся между версиями API).
    """
    if not (merchant_id and api_key):
        raise BillingError("platega not configured (PLATEGA_MERCHANT_ID/PLATEGA_API_KEY)")
    if plan not in PRICES:
        raise BillingError(f"plan must be {'|'.join(PRICES)}")
    currency = (currency or os.environ.get("PLATEGA_CURRENCY") or "RUB").upper()
    amount = PRICES[plan].get(currency)
    if amount is None:
        raise BillingError(f"unsupported currency {currency} (есть: {sorted(PRICES[plan])})")
    post = http_post or _http_post_json
    body = {
        "paymentDetails": {"amount": amount, "currency": currency},
        "description": LABELS[plan],
        "return": return_url,
        "failedUrl": failed_url or return_url,
        "payload": payload or email,          # вернётся в callback — наш ref/email
        "metadata": {"userId": email},        # антифрод Platega: идентификатор плательщика
    }
    try:
        resp = post(f"{api_base.rstrip('/')}/v2/transaction/process",
                    {"X-MerchantId": merchant_id, "X-Secret": api_key}, body)
    except BillingError:
        raise
    except Exception as e:
        raise BillingError(f"platega request failed: {e}") from e
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    pid = str(data.get("transactionId") or data.get("id") or "")
    url = data.get("url") or data.get("redirect") or data.get("link") or data.get("paymentUrl")
    if not (pid and url):
        raise BillingError(f"platega: no transactionId/url in response: {json.dumps(resp)[:300]}")
    return {"payment_id": pid, "payment_url": url}


# --------------------------------------------------------------- delivery ---

def send_email_smtp(to_addr: str, subject: str, text: str, cfg: dict, smtp_factory=None,
                    html: str | None = None):
    """SMTP + STARTTLS (порт 465 → SSL). cfg: host/port/user/password/sender/starttls.
    starttls="0" — plain SMTP (локальный postfix в docker-сети). Raises on failure.
    html (v0.32.0) — если задан, письмо уходит multipart/alternative: text + html."""
    host = cfg.get("host") or ""
    if not host:
        raise BillingError("SMTP_HOST не задан")
    port = int(cfg.get("port") or 587)
    msg = EmailMessage()
    sender = cfg.get("sender") or cfg.get("user") or "maxgraph@localhost"
    msg["From"] = sender if "<" in sender else f"MaxGraph <{sender}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if port == 465:
        factory = smtp_factory or smtplib.SMTP_SSL
        s = factory(host, port, timeout=20)
    else:
        factory = smtp_factory or smtplib.SMTP
        s = factory(host, port, timeout=20)
        if str(cfg.get("starttls", "1")).strip().lower() not in ("0", "no", "false"):
            s.ehlo()
            s.starttls()
            s.ehlo()
    try:
        if cfg.get("user"):
            s.login(cfg["user"], cfg.get("password") or "")
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def send_telegram(chat_id: str, text: str, token: str, http_post=None) -> dict:
    """Telegram Bot API sendMessage. Raises on failure."""
    if not token:
        raise BillingError("TG_BOT_TOKEN не задан")
    post = http_post or _http_post_json
    return post(f"https://api.telegram.org/bot{token}/sendMessage", {},
                {"chat_id": chat_id, "text": text})


def delivery_text(code: str, license_key: str, tier: str) -> str:
    """Письмо покупателю (v0.18.0): главный артефакт — ключ активации (mg_…),
    резервный код MG-… — для восстановления ключа. Без путаницы «код vs ключ»."""
    return (
        f"Maxgraph {LABELS.get(tier, tier)} — оплата получена ✨\n\n"
        f"Твой ключ активации: {license_key}\n\n"
        f"Куда вставить: дашборд MaxGraph → статус лицензии вверху "
        f"(или шестерёнка → «Лицензия») → вставь ключ → «Активировать».\n"
        f"Если покупал прямо из дашборда — ключ уже активирован автоматически.\n\n"
        f"Резервный код: {code} — сохрани: по нему поддержка восстановит ключ,\n"
        f"если потеряешь (help@rezon-ai.ru)."
    )


# ------------------------------------------------------ html-письма (v0.32.0) ---
# Светлая «тёплая» карточка в палитре лендинга (мокап designs/maxgraph-email-v1,
# ревизия дизайн-критиком). Только таблицы + inline-стили: Gmail/Outlook режут
# flex/grid/<style>. Без картинок и web-шрифтов — доставляемость.

def _esc(s) -> str:
    return _html.escape(str(s), quote=True)


def _email_frame(*, preheader: str, card_inner: str, footer_note: str) -> str:
    """Общий каркас письма: тёплая подложка, белая карточка 600px, футер.
    card_inner — готовые <tr> содержимого карточки (значения уже сэскейплены).
    preheader/footer_note НЕ эскейпятся — только доверенные константы из кода."""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="only light">
<meta name="supported-color-schemes" content="light">
<title>MaxGraph</title>
</head>
<body style="margin:0;padding:0">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="#F7F3EC" style="background:#F7F3EC">
<tr><td align="center" style="padding:32px 12px">
  <span style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;color:#F7F3EC">{preheader}</span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px">
    <tr><td style="padding:0 0 24px">
      <span style="font:700 20px/1.2 Arial,Helvetica,sans-serif;color:#191613">Max<span style="color:#B47721">Graph</span></span><span style="font:700 20px/1.2 Arial,Helvetica,sans-serif;color:#D98E32">.</span>
    </td></tr>
    <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;border:1px solid #EAE2D5;border-radius:12px;padding:36px 32px">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
{card_inner}
      </table>
    </td></tr>
    <tr><td style="padding:24px 0 0;font:400 12px/1.6 Arial,Helvetica,sans-serif;color:#6E6355" align="center">
      Вопросы — <a href="mailto:help@rezon-ai.ru" style="color:#6E6355;text-decoration:underline">help@rezon-ai.ru</a>
      &nbsp;·&nbsp; <a href="https://rezon-ai.ru" style="color:#6E6355;text-decoration:underline">rezon-ai.ru</a>
    </td></tr>
    <tr><td style="padding:8px 0 0;font:400 11px/1.5 Arial,Helvetica,sans-serif;color:#6E6355" align="center">
      {footer_note}
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def delivery_html(code: str, license_key: str, success_url: str | None = None) -> str:
    """HTML-письмо покупки: ключ + код моно-блоками, кнопка страницы заказа
    (только при переданном success_url). Plain-версия — delivery_text (не меняется:
    она же уходит в Telegram и как text/plain часть письма)."""
    button_rows = ""
    if success_url:
        button_rows = f"""
        <tr><td align="center" style="padding:0 0 8px">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
            <td bgcolor="#D98E32" style="background:#D98E32;border-radius:8px">
              <a href="{_esc(success_url)}" style="display:block;padding:14px 32px;font:700 15px/1.4 Arial,Helvetica,sans-serif;color:#191613;text-decoration:none;border-radius:8px">Открыть страницу заказа</a>
            </td>
          </tr></table>
        </td></tr>
        <tr><td align="center" style="font:400 12px/1.5 Arial,Helvetica,sans-serif;color:#7A6F60;padding:0 0 4px">
          Страница заказа остаётся рабочей — ключ и код всегда там
        </td></tr>"""
    card_inner = f"""        <tr><td style="font:700 24px/1.3 Arial,Helvetica,sans-serif;color:#191613;padding:0 0 8px">
          Оплата прошла&nbsp;✨
        </td></tr>
        <tr><td style="font:400 15px/1.6 Arial,Helvetica,sans-serif;color:#3D362E;padding:0 0 24px">
          MaxGraph Pro оплачен. Ключ и код — ниже, и они же ждут на странице заказа (ссылка в конце письма). Сохрани их:
        </td></tr>
        <tr><td style="font:700 11px/1.4 Arial,Helvetica,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#7A6F60;padding:0 0 6px">
          Ключ активации
        </td></tr>
        <tr><td bgcolor="#FBF8F2" style="background:#FBF8F2;border:1px solid #EAE2D5;border-radius:8px;padding:14px 16px;font:400 14px/1.6 'Courier New',Courier,monospace;color:#191613;word-break:break-all">
          {_esc(license_key)}
        </td></tr>
        <tr><td style="font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#7A6F60;padding:8px 0 24px">
          Вставляется в дашборде MaxGraph: статус лицензии вверху → «Активировать». Если покупал из дашборда — ключ уже активирован сам.
        </td></tr>
        <tr><td style="font:700 11px/1.4 Arial,Helvetica,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#7A6F60;padding:0 0 6px">
          Код кабинета
        </td></tr>
        <tr><td bgcolor="#FBF8F2" style="background:#FBF8F2;border:1px solid #EAE2D5;border-radius:8px;padding:14px 16px;font:700 15px/1.5 'Courier New',Courier,monospace;color:#191613;letter-spacing:.04em">
          {_esc(code)}
        </td></tr>
        <tr><td style="font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#7A6F60;padding:8px 0 24px">
          Резервный. По нему поддержка восстановит ключ, если потеряешь.
        </td></tr>{button_rows}"""
    return _email_frame(preheader="Ключ активации MaxGraph Pro внутри",
                        card_inner=card_inner,
                        footer_note="Письмо отправлено автоматически после оплаты — отвечать на него не нужно.")


def recovery_html(key: str) -> str:
    """HTML-письмо восстановления ключа (новый ключ после revoke старого)."""
    card_inner = f"""        <tr><td style="font:700 24px/1.3 Arial,Helvetica,sans-serif;color:#191613;padding:0 0 8px">
          Восстановление ключа
        </td></tr>
        <tr><td style="font:400 15px/1.6 Arial,Helvetica,sans-serif;color:#3D362E;padding:0 0 24px">
          Ты запросил восстановление — вот твой новый ключ MaxGraph:
        </td></tr>
        <tr><td style="font:700 11px/1.4 Arial,Helvetica,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#7A6F60;padding:0 0 6px">
          Новый ключ активации
        </td></tr>
        <tr><td bgcolor="#FBF8F2" style="background:#FBF8F2;border:1px solid #EAE2D5;border-radius:8px;padding:14px 16px;font:400 14px/1.6 'Courier New',Courier,monospace;color:#191613;word-break:break-all">
          {_esc(key)}
        </td></tr>
        <tr><td style="font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#7A6F60;padding:8px 0 24px">
          Вставляется в дашборде MaxGraph: статус лицензии вверху → «Активировать». Старый ключ уже заменён и не работает — сохрани это письмо.
        </td></tr>
        <tr><td style="border-top:1px solid #F0E9DC;padding-top:20px;font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#7A6F60">
          Не запрашивал восстановление? Напиши нам: <a href="mailto:help@rezon-ai.ru" style="color:#7A6F60;text-decoration:underline">help@rezon-ai.ru</a>
        </td></tr>"""
    return _email_frame(preheader="Новый ключ MaxGraph внутри — старый больше не работает",
                        card_inner=card_inner,
                        footer_note="Письмо отправлено автоматически — отвечать на него не нужно.")


def deliver_code(*, code: str, license_key: str, tier: str, email: str | None = None,
                 tg_chat_id: str | None = None, success_url: str | None = None,
                 env=None, smtp_factory=None, http_post=None, logger=None) -> dict:
    """Доставка кода+license key по всем настроенным каналам. НИКОГДА не бросает.

    → {"email": "sent|skipped|error: ...", "telegram": ...}
    Запись в codes.json / success-страница — вне этой функции (см. app.py).
    success_url (v0.32.0) — ссылка на success-страницу для кнопки в html-письме.
    """
    env = os.environ if env is None else env
    logger = logger or log
    text = delivery_text(code, license_key, tier)
    out = {}

    if email and env.get("SMTP_HOST"):
        try:
            send_email_smtp(email, f"Maxgraph {tier}: ключ активации", text, {
                "host": env.get("SMTP_HOST"), "port": env.get("SMTP_PORT"),
                "user": env.get("SMTP_USER"), "password": env.get("SMTP_PASS"),
                "sender": env.get("SMTP_FROM") or env.get("SMTP_USER"),
                "starttls": env.get("SMTP_STARTTLS"),
            }, smtp_factory=smtp_factory,
                html=delivery_html(code, license_key, success_url=success_url))
            out["email"] = "sent"
        except Exception as e:
            logger.error("deliver_code email → %s: %s", email, e)
            out["email"] = f"error: {e}"
    else:
        out["email"] = "skipped"

    if tg_chat_id and env.get("TG_BOT_TOKEN"):
        try:
            send_telegram(tg_chat_id, text, env["TG_BOT_TOKEN"], http_post=http_post)
            out["telegram"] = "sent"
        except Exception as e:
            logger.error("deliver_code telegram → %s: %s", tg_chat_id, e)
            out["telegram"] = f"error: {e}"
    else:
        out["telegram"] = "skipped"

    if all(v != "sent" for v in out.values()):
        logger.warning("deliver_code: ни один канал не доставил (code=%s); "
                       "код доступен на success-странице и в codes.json", code)
    return out
