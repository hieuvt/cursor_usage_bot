"""Load and validate config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_LOCALE = "vi"


class ConfigError(Exception):
    """Raised when config file is missing, invalid, or incomplete."""


@dataclass(frozen=True)
class CursorConfig:
    session_token: str
    timezone: str = DEFAULT_TIMEZONE


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class ReportConfig:
    locale: str = DEFAULT_LOCALE


@dataclass(frozen=True)
class AppConfig:
    cursor: CursorConfig
    telegram: TelegramConfig
    report: ReportConfig


def _require_non_empty_str(data: dict[str, Any], key: str, path: str) -> str:
    if key not in data or data[key] is None:
        raise ConfigError(f"Missing required config field: {path}.{key}")
    value = data[key]
    if not isinstance(value, (str, int)):
        raise ConfigError(f"Config field {path}.{key} must be a string")
    text = str(value).strip()
    if not text:
        raise ConfigError(f"Config field {path}.{key} must not be empty")
    return text


def _as_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{path}' must be a mapping")
    return value


def load_config(path: str | Path) -> AppConfig:
    """Load and validate YAML config from ``path``."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping: {config_path}")

    cursor_raw = _as_mapping(raw.get("cursor"), "cursor")
    telegram_raw = _as_mapping(raw.get("telegram"), "telegram")
    report_raw = raw.get("report") or {}
    if report_raw is None:
        report_raw = {}
    report_raw = _as_mapping(report_raw, "report")

    session_token = _require_non_empty_str(cursor_raw, "session_token", "cursor")
    timezone = str(cursor_raw.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE

    bot_token = _require_non_empty_str(telegram_raw, "bot_token", "telegram")
    chat_id = _require_non_empty_str(telegram_raw, "chat_id", "telegram")

    locale = str(report_raw.get("locale") or DEFAULT_LOCALE).strip() or DEFAULT_LOCALE

    return AppConfig(
        cursor=CursorConfig(session_token=session_token, timezone=timezone),
        telegram=TelegramConfig(bot_token=bot_token, chat_id=chat_id),
        report=ReportConfig(locale=locale),
    )
