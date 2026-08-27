"""Cursor daily usage reporter — personal account → Telegram."""

from cursor_usage.aggregator import (
    ModelUsage,
    ReportWindow,
    UsageReport,
    build_report,
    classify_model_pool,
    compute_report_window,
)
from cursor_usage.config import AppConfig, ConfigError, load_config
from cursor_usage.cursor_client import (
    CursorAPIError,
    CursorAuthError,
    CursorClient,
    CursorSchemaError,
)
from cursor_usage.formatter import format_error_alert, format_report
from cursor_usage.telegram_sender import TelegramError, send_message

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "ConfigError",
    "CursorAPIError",
    "CursorAuthError",
    "CursorClient",
    "CursorSchemaError",
    "ModelUsage",
    "ReportWindow",
    "TelegramError",
    "UsageReport",
    "build_report",
    "classify_model_pool",
    "compute_report_window",
    "format_error_alert",
    "format_report",
    "load_config",
    "send_message",
    "__version__",
]
