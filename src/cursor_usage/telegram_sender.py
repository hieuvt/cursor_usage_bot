"""Send messages via Telegram Bot API."""

from __future__ import annotations

import httpx

TELEGRAM_MAX_MESSAGE_LEN = 4096


class TelegramError(Exception):
    """Raised when Telegram Bot API request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 30.0,
) -> None:
    """POST sendMessage; split text if longer than Telegram's limit."""
    if not bot_token.strip():
        raise TelegramError("telegram bot_token is empty")
    if not str(chat_id).strip():
        raise TelegramError("telegram chat_id is empty")

    chunks = split_message(text, TELEGRAM_MAX_MESSAGE_LEN)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    with httpx.Client(timeout=timeout) as client:
        for chunk in chunks:
            _send_chunk(client, url, chat_id, chunk)


def split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Split text into chunks <= max_len, preferring newlines."""
    if len(text) <= max_len:
        return [text] if text else [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        window = remaining[:max_len]
        # Prefer section / line breaks near the end of the window
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < max_len // 2:
            cut = max_len
        chunk = remaining[:cut].rstrip("\n")
        if not chunk:
            chunk = remaining[:max_len]
            cut = len(chunk)
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")

    return chunks


def _send_chunk(
    client: httpx.Client,
    url: str,
    chat_id: str,
    text: str,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise TelegramError("Timeout calling Telegram sendMessage") from exc
    except httpx.RequestError as exc:
        raise TelegramError(f"Network error calling Telegram: {exc}") from exc

    body = response.text
    if response.status_code != 200:
        raise TelegramError(
            f"Telegram HTTP {response.status_code}: {body[:300]}",
            status_code=response.status_code,
            response_body=body,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramError(
            f"Telegram returned non-JSON: {body[:300]}",
            status_code=response.status_code,
            response_body=body,
        ) from exc

    if not data.get("ok"):
        desc = data.get("description") or body[:300]
        raise TelegramError(
            f"Telegram API error: {desc}",
            status_code=response.status_code,
            response_body=body,
        )
