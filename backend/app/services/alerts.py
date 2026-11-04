"""Operational alerts to a chat webhook (Slack- and Discord-compatible).

ALERT_WEBHOOK_URL unset → alerts are only logged. Sending happens on a
background thread so a slow or dead webhook never delays a render, and
identical alerts are de-duplicated for ALERT_DEDUPE_MINUTES.

Raised for: render / loop / publish failures, a burst of provider errors
(ALERT_PROVIDER_ERRORS errors from one provider within 10 min), and an
active model disappearing from the polled catalog.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import httpx

from ..config import get_settings

log = logging.getLogger("avs.alerts")

_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_provider_errors: dict[str, deque] = defaultdict(deque)
_sent_log: deque = deque(maxlen=50)  # recent alerts, for tests / status
PROVIDER_WINDOW_SECONDS = 600


def _post(url: str, text: str) -> None:
    try:
        # "text" = Slack/Mattermost, "content" = Discord.
        httpx.post(url, json={"text": text, "content": text}, timeout=10.0)
    except Exception as e:  # noqa: BLE001
        log.warning("alert webhook failed: %s", e)


def send(key: str, title: str, detail: str = "", *, severity: str = "error", sync: bool = False) -> bool:
    """Send an alert unless an identical `key` was sent recently.
    Returns True if it was sent (or would have been, with no webhook)."""
    s = get_settings()
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < s.alert_dedupe_minutes * 60:
            return False
        _last_sent[key] = now
    icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(severity, "•")
    text = f"{icon} AvatarVideoStudio · {title}"
    if detail:
        text += f"\n{detail[:1500]}"
    _sent_log.append({"key": key, "title": title, "severity": severity, "text": text})
    log.log(logging.ERROR if severity == "error" else logging.WARNING, "ALERT %s: %s %s", key, title, detail[:300])
    if s.alert_webhook_url:
        if sync:
            _post(s.alert_webhook_url, text)
        else:
            threading.Thread(target=_post, args=(s.alert_webhook_url, text), daemon=True).start()
    return True


def provider_error(provider: str, endpoint: str, error: str) -> None:
    """Count a provider failure; alert when they burst."""
    s = get_settings()
    now = time.monotonic()
    with _lock:
        q = _provider_errors[provider]
        q.append(now)
        while q and now - q[0] > PROVIDER_WINDOW_SECONDS:
            q.popleft()
        count = len(q)
    if count >= s.alert_provider_errors:
        send(
            f"provider-burst:{provider}",
            f"{provider} failing: {count} errors in 10 min",
            f"last: {endpoint} — {error[:300]}",
            severity="error",
        )


def render_failed(kind: str, project_id: int, title: str, error: str) -> None:
    send(f"{kind}-failed:{project_id}", f"{kind} failed: “{title or project_id}” (#{project_id})", error[:800])


def recent() -> list[dict]:
    return list(_sent_log)


def reset() -> None:
    with _lock:
        _last_sent.clear()
        _provider_errors.clear()
        _sent_log.clear()
