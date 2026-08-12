"""Format UsageReport as Telegram HTML messages."""

from __future__ import annotations

from datetime import datetime

from cursor_usage.aggregator import ModelUsage, UsageReport
from cursor_usage.config import ConfigError
from cursor_usage.cursor_client import CursorAPIError, CursorAuthError, CursorSchemaError

ALERT_PREFIX = "⚠️ Cursor Usage — CẢNH BÁO"


def format_report(report: UsageReport) -> str:
    """Build a Vietnamese HTML report for Telegram."""
    lines: list[str] = [
        f"<b>Cursor Usage - {_esc(report.report_date.isoformat())}</b>",
        (
            f"Chu kỳ: {_fmt_date(report.billing_cycle_start)} - "
            f"{_fmt_date(report.billing_cycle_end)}"
        ),
        "",
        "<b>--- thông tin gói ---</b>",
        f"tên gói: <code>{_esc(report.membership_type)}</code>",
        f"số tiền included: {_fmt_usd(report.included_money_usd)}",
        f"số plan unit included: {_fmt_units(report.included_plan_units)}",
        f"tương ứng: {_fmt_usd_per_unit(report.usd_per_plan_unit)} / 1 plan unit",
        f"số plan unit bonus: {_fmt_units(report.bonus_plan_units)}",
        "",
        "<b>--- Tổng số tiền đã tiêu từ đầu chu kỳ ---</b>",
        f"số plan unit đã dùng từ đầu chu kỳ: {_fmt_units(report.cycle_plan_units)}",
        f"số tiền đã tiêu từ đầu chu kỳ: {_fmt_usd(report.cycle_spent_usd)}",
        (
            f"số plan unit còn trong gói included: "
            f"{_fmt_units(report.included_remaining_units)}"
        ),
        f"số tiền included còn lại: {_fmt_usd(report.included_remaining_usd)}",
        f"số plan unit còn trong bonus: {_fmt_units(report.bonus_remaining_units)}",
        f"số tiền on demand đã trả: {_fmt_usd(report.on_demand_usd)}",
        (
            f"ước tính số plan unit dùng đến hết chu kỳ: "
            f"{_fmt_units(report.projected_eoc_plan_units)}"
            f" <i>({report.days_elapsed} ngày đã qua, "
            f"còn {report.remaining_days} ngày)</i>"
        ),
        (
            f"ước tính số tiền on demand cần trả đến hết chu kỳ: "
            f"{_fmt_usd(report.estimated_on_demand_eoc_usd)}"
        ),
        "",
        "<b>--- Tổng số tiền đã tiêu ngày hôm qua ---</b>",
        f"số plan unit đã dùng ngày hôm qua: {_fmt_units(report.yesterday_plan_units)}",
        f"số tiền đã tiêu ngày hôm qua: {_fmt_usd(report.yesterday_spent_usd)}",
        "",
        "<b>--- Chi tiết model được sử dụng ngày hôm qua ---</b>",
        _format_model_table(report.yesterday),
        "",
        "<b>--- Chi tiết model được sử dụng từ đầu chu kỳ ---</b>",
        _format_model_table(report.cycle_to_date),
    ]
    return "\n".join(lines)


def format_error_alert(error: BaseException) -> str:
    """Short operational alert (SPEC §6.1)."""
    if isinstance(error, CursorAuthError):
        detail = (
            "Cookie/session Cursor hết hạn hoặc không hợp lệ.\n"
            "Hãy cập nhật <code>cursor.session_token</code> trong <code>config.yaml</code>."
        )
    elif isinstance(error, CursorSchemaError):
        endpoint = getattr(error, "endpoint", None) or "?"
        detail = (
            "Response Cursor thiếu field / lệch schema — "
            "<b>có thể API dashboard đã thay đổi</b>.\n"
            f"Endpoint: <code>{_esc(str(endpoint))}</code>\n"
            f"Chi tiết: {_esc(str(error))}"
        )
    elif isinstance(error, CursorAPIError):
        endpoint = getattr(error, "endpoint", None) or "?"
        status = getattr(error, "status_code", None)
        msg = str(error).lower()
        if "timeout" in msg or "network" in msg:
            detail = (
                "Không kết nối được tới Cursor (timeout/mạng).\n"
                f"Endpoint: <code>{_esc(str(endpoint))}</code>\n"
                f"Chi tiết: {_esc(str(error))}"
            )
        else:
            status_part = f" HTTP {status}" if status is not None else ""
            detail = (
                f"Lỗi Cursor API{status_part} — "
                "<b>có thể API dashboard đã thay đổi</b>.\n"
                f"Endpoint: <code>{_esc(str(endpoint))}</code>\n"
                f"Chi tiết: {_esc(str(error))}"
            )
    elif isinstance(error, ConfigError):
        detail = f"Lỗi cấu hình: {_esc(str(error))}"
    else:
        detail = (
            f"Lỗi không xác định: <code>{_esc(type(error).__name__)}</code> — "
            f"{_esc(str(error))}"
        )

    return f"<b>{ALERT_PREFIX}</b>\n\n{detail}"


def _format_model_table(models: list[ModelUsage]) -> str:
    """Render a monospace table (Telegram has no markdown tables)."""
    if not models:
        return "<pre>Không có usage</pre>"

    headers = ("Model", "Tokens", "Plan unit", "USD")
    rows: list[tuple[str, str, str, str]] = [
        (
            m.model,
            _fmt_int(m.total_tokens),
            _fmt_units_plain(m.plan_units),
            _fmt_usd_plain(m.cost_usd),
        )
        for m in models
    ]
    total_tokens = sum(m.total_tokens for m in models)
    total_units = sum(m.plan_units for m in models)
    total_usd = sum(m.cost_usd for m in models)
    rows.append(
        (
            "TỔNG",
            _fmt_int(total_tokens),
            _fmt_units_plain(total_units),
            _fmt_usd_plain(total_usd),
        )
    )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: tuple[str, ...] | list[str]) -> str:
        return " | ".join(str(cells[i]).ljust(widths[i]) for i in range(4))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep, *[fmt_row(r) for r in rows]]
    return "<pre>" + _esc("\n".join(lines)) + "</pre>"


def _fmt_date(dt: datetime) -> str:
    return _esc(dt.strftime("%Y-%m-%d"))


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_units(value: float | None) -> str:
    if value is None:
        return "?"
    return _fmt_units_plain(value)


def _fmt_units_plain(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "?"
    return _fmt_usd_plain(value)


def _fmt_usd_plain(value: float) -> str:
    abs_v = abs(value)
    if abs_v == 0:
        return "$0.00"
    if abs_v < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _fmt_usd_per_unit(value: float | None) -> str:
    if value is None:
        return "?"
    # Show more precision for small rates (e.g. Ultra $200/40000 = $0.005)
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:.4f}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
