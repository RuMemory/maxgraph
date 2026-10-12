"""ghhook (v0.33.0) — приёмник GitHub-вебхуков витрины → TG-уведомление владельцу.

Чистый stdlib, без сети: проверка подписи HMAC-SHA256 и форматирование событий.
Сеть (Telegram) делает вызывающий (app.py) через billing.send_telegram.
"""

import hashlib
import hmac


def verify_signature(secret: str, body: bytes, sig_header: str) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex>'. Пустой секрет/подпись/чужой префикс → False."""
    if not secret or not sig_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig_header[7:])


def _clip(value, limit: int = 120) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def _login(payload: dict) -> str:
    return _clip((payload.get("sender") or {}).get("login") or "?", 60)


def _repo(payload: dict) -> str:
    return _clip((payload.get("repository") or {}).get("full_name") or "repo", 60)


def format_telegram(event: str, payload: dict) -> str:
    """Короткая строка для TG. Пустая строка = событие не интересует (не шлём)."""
    repo, who, action = _repo(payload), _login(payload), payload.get("action") or ""
    if event == "ping":
        return f"🔗 GitHub webhook подключён к {repo}"
    if event == "watch" and action == "started":
        total = (payload.get("repository") or {}).get("stargazers_count", "?")
        return f"⭐ {who} поставил звезду {repo} (всего {total})"
    if event == "fork":
        total = (payload.get("repository") or {}).get("forks_count", "?")
        return f"🍴 {who} форкнул {repo} (всего {total})"
    if event == "issues":
        issue = payload.get("issue") or {}
        num = issue.get("number", "?")
        if action == "opened":
            return f"🐞 {who} открыл issue #{num} в {repo}: {_clip(issue.get('title'))}"
        if action == "closed":
            return f"✅ {who} закрыл issue #{num} в {repo}"
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        num = pr.get("number", "?")
        if action == "opened":
            return f"🔀 {who} открыл PR #{num} в {repo}: {_clip(pr.get('title'))}"
        if action == "closed" and pr.get("merged"):
            return f"🎉 PR #{num} в {repo} влит ({who})"
    if event == "release" and action == "published":
        tag = (payload.get("release") or {}).get("tag_name", "?")
        return f"🚀 Релиз {_clip(tag, 40)} в {repo}"
    return ""
