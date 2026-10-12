"""Container health probe for the configured local Telegram Bot API."""

from __future__ import annotations

import ipaddress
from urllib.parse import quote, urlsplit

import httpx

from .config import settings

HEALTHCHECK_TIMEOUT = 2.0


def _is_local_api_root(api_root: str) -> bool:
    try:
        parsed = urlsplit(api_root)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False

    local_root = loopback and parsed.scheme in {"http", "https"} and parsed.path in {"", "/"}
    teya_root = (
        host == "telegram-bot-api"
        and parsed.scheme == "http"
        and port == 8081
        and parsed.path in {"", "/"}
    )
    return bool(
        (local_root or teya_root)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def check_health() -> bool:
    token = settings.telegram_bot_token
    api_root = settings.telegram_api_root
    if not token or not api_root or not _is_local_api_root(api_root):
        return False

    endpoint = f"{api_root.rstrip('/')}/bot{quote(token, safe=':')}/getMe"
    try:
        response = httpx.get(
            endpoint,
            timeout=HEALTHCHECK_TIMEOUT,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("ok") is True
        and isinstance(payload.get("result"), dict)
        and payload["result"].get("is_bot") is True
    )


def main() -> int:
    return 0 if check_health() else 1


if __name__ == "__main__":
    raise SystemExit(main())
