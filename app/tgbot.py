#!/usr/bin/env python3
"""Telegram-бот Maxgraph: long-poll getUpdates, deep-link /start <payload>.

Только stdlib (urllib) — модуль тестируется отдельно от сервера.

Бот НЕ пишет первым (ограничение Bot API): покупатель приходит по ссылке
t.me/<bot>?start=<ref>, бот отвечает кодом покупки. Со success-страницы ссылка
убрана в v0.27.2 (бот не работает с покупателями) — обработчик оставлен для
старых ссылок; основная роль бота — уведомления владельцу о покупках.
Бизнес-логика инжектится из app.py как on_start(chat_id, payload) -> text;
должна быть идемпотентной (после рестарта offset сбрасывается — старые апдейты
придут повторно, пока не подтверждены).
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("maxgraph.tgbot")
API = "https://api.telegram.org"


def parse_start(message: dict) -> tuple[str, str] | None:
    """message → (chat_id, payload) для '/start <payload>', иначе None.

    Принимаем '/start', '/start <payload>', '/start@BotName <payload>'.
    """
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    head = parts[0].split("@", 1)[0]
    if head != "/start":
        return None
    return str(chat_id), (parts[1].strip() if len(parts) > 1 else "")


def _api_call(token: str, method: str, params: dict, timeout: int = 35) -> dict:
    url = f"{API}/bot{token}/{method}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class BotPoller:
    """Long-poll getUpdates в daemon-потоке. offset в памяти (см. docstring модуля)."""

    def __init__(self, token: str, on_start, timeout: int = 30, api_call=None):
        self.token = token
        self.on_start = on_start
        self.timeout = timeout
        self.offset = 0
        self._stop = threading.Event()
        self._api = api_call or _api_call  # инъекция для тестов

    def poll_once(self) -> int:
        """Одна итерация getUpdates → число обработанных апдейтов. Raises on network error."""
        j = self._api(self.token, "getUpdates",
                      {"offset": self.offset, "timeout": self.timeout},
                      timeout=self.timeout + 10)
        updates = j.get("result", []) if isinstance(j, dict) else []
        for upd in updates:
            self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
            parsed = parse_start(upd.get("message") or {})
            if not parsed:
                continue
            chat_id, payload = parsed
            try:
                text = self.on_start(chat_id, payload) or "ok"
            except Exception as e:
                log.error("tgbot on_start упал (chat %s): %s", chat_id, e)
                text = "⚠️ Ошибка — попробуй ещё раз через минуту."
            try:
                self._api(self.token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
            except Exception as e:
                log.error("tgbot sendMessage → %s: %s", chat_id, e)
        return len(updates)

    def run_forever(self):
        log.info("tgbot: long-poll запущен (timeout=%ss)", self.timeout)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.warning("tgbot getUpdates: %s — retry через 5с", e)
                self._stop.wait(5)

    def stop(self):
        self._stop.set()


def start_poller(token: str, on_start, timeout: int = 30) -> BotPoller:
    """Запустить фоновый getUpdates-поллер. Возвращает BotPoller (для stop() в тестах)."""
    poller = BotPoller(token, on_start, timeout=timeout)
    threading.Thread(target=poller.run_forever, daemon=True, name="tgbot-poller").start()
    return poller
