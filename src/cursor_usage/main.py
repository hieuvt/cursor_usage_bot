"""CLI orchestration: fetch Cursor usage → report → Telegram."""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cursor_usage.aggregator import build_report, compute_report_window
from cursor_usage.config import AppConfig, ConfigError, load_config
from cursor_usage.cursor_client import (
    CursorAPIError,
    CursorAuthError,
    CursorClient,
    CursorSchemaError,
)
from cursor_usage.formatter import format_error_alert, format_report
from cursor_usage.telegram_sender import TelegramError, send_message

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dry_run: bool = args.dry_run
    config_path = Path(args.config)

    cfg: AppConfig | None = None
    try:
        cfg = load_config(config_path)
        message = _build_daily_message(cfg)
        if dry_run:
            print(message)
            return EXIT_OK
        send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, message)
        return EXIT_OK
    except CursorAuthError as exc:
        return _handle_failure(exc, cfg=cfg, dry_run=dry_run, exit_code=EXIT_AUTH)
    except (CursorSchemaError, CursorAPIError, ConfigError, TelegramError) as exc:
        return _handle_failure(exc, cfg=cfg, dry_run=dry_run, exit_code=EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 — last-resort alert for cron
        return _handle_failure(exc, cfg=cfg, dry_run=dry_run, exit_code=EXIT_ERROR)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cursor_usage",
        description="Daily Cursor usage report → Telegram group",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report to stdout; do not send Telegram",
    )
    return parser.parse_args(argv)


def _build_daily_message(cfg: AppConfig) -> str:
    now = datetime.now(ZoneInfo(cfg.cursor.timezone))
    with CursorClient(cfg.cursor.session_token) as client:
        summary = client.get_usage_summary()
        window = compute_report_window(
            summary, timezone_name=cfg.cursor.timezone, now=now
        )
        period_usage = client.get_current_period_usage()
        yesterday_aggregations = client.get_aggregated_usage(
            _epoch_ms(window.yesterday_start),
            _epoch_ms(window.yesterday_end),
        )
        projection_aggregations = client.get_aggregated_usage(
            window.fetch_start_ms,
            window.fetch_end_ms,
        )
        cycle_aggregations = client.get_aggregated_usage(
            _epoch_ms(window.cycle_start),
            _epoch_ms(now),
        )
        usage_events = list(
            client.iter_usage_events(
                _epoch_ms(window.cycle_start),
                _epoch_ms(now),
            )
        )
    report = build_report(
        summary,
        period_usage=period_usage,
        yesterday_aggregations=yesterday_aggregations,
        cycle_aggregations=cycle_aggregations,
        projection_aggregations=projection_aggregations,
        timezone=cfg.cursor.timezone,
        now=now,
        usage_events=usage_events,
    )
    return format_report(report)


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _handle_failure(
    exc: BaseException,
    *,
    cfg: AppConfig | None,
    dry_run: bool,
    exit_code: int,
) -> int:
    print(f"ERROR: {exc}", file=sys.stderr)
    if not isinstance(exc, (CursorAuthError, CursorAPIError, ConfigError, TelegramError)):
        traceback.print_exc(file=sys.stderr)

    alert = format_error_alert(exc)
    if dry_run:
        print(alert)
        return exit_code

    if cfg is not None:
        try:
            send_message(cfg.telegram.bot_token, cfg.telegram.chat_id, alert)
        except Exception as send_exc:  # noqa: BLE001
            print(f"ERROR: failed to send Telegram alert: {send_exc}", file=sys.stderr)
    else:
        print(
            "ERROR: cannot send Telegram alert (config not loaded)",
            file=sys.stderr,
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
