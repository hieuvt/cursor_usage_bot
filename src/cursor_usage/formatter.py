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
        f"<b>📊 Cursor Usage — {_esc(report.report_date.isoformat())}</b>",
        "",
        f"🧾 Chu kỳ: {_fmt_dt(report.billing_cycle_start)} → {_fmt_dt(report.billing_cycle_end)}",
        f"📦 Gói: <code>{_esc(report.membership_type)}</code>",
        f"🎁 Khuyến mại: {_esc(report.promo_note)}",
    ]

    if report.plan_used is not None or report.plan_limit is not None:
        used = _fmt_number(report.plan_used) if report.plan_used is not None else "?"
        limit = _fmt_number(report.plan_limit) if report.plan_limit is not None else "?"
        lines.append(f"📈 Plan (cycle): {used} / {limit}")

    lines.extend(["", *_format_money_section(report)])

    lines.extend(
        [
            "",
            "<b>—— Hôm qua ——</b>",
            *_format_model_section(
                report.yesterday,
                report.yesterday_total_tokens,
                report.yesterday_total_cost_usd,
            ),
            "",
            "<b>—— Từ đầu chu kỳ → hết hôm qua ——</b>",
            *_format_model_section(
                report.cycle_to_date,
                report.cycle_total_tokens,
                report.cycle_total_cost_usd,
            ),
        ]
    )
    return "\n".join(lines)


def _format_money_section(report: UsageReport) -> list[str]:
    """Subscription / promo / on-demand estimates."""
    bonus_txt = (
        _fmt_usd(report.promo_bonus_usd)
        if report.promo_bonus_usd is not None
        else "Không có"
    )
    sub_txt = (
        _fmt_usd(report.subscription_usd)
        if report.subscription_usd is not None
        else "?"
    )
    included_txt = (
        _fmt_usd(report.included_usd)
        if report.included_usd is not None
        else "?"
    )

    sub_plus_promo = None
    if report.subscription_usd is not None:
        sub_plus_promo = report.subscription_usd + (report.promo_bonus_usd or 0.0)

    lines = [
        "<b>💵 Ước tính tiền</b> <i>(1 đơn vị plan ≈ $0.01)</i>",
        f"• Khuyến mại (bonus): {bonus_txt}",
        f"• Gói cước (subscription): {sub_txt}",
        f"• Included kèm gói: {included_txt}",
    ]
    if sub_plus_promo is not None:
        lines.append(
            f"• Gói cước + khuyến mại: {_fmt_usd(sub_plus_promo)}"
        )
    lines.append(f"• On-demand (ước tính): {_fmt_usd(report.on_demand_usd)}")
    lines.append(
        f"• Gross usage chu kỳ: {_fmt_usd(report.cycle_total_cost_usd)}"
    )
    return lines


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
        detail = f"Lỗi không xác định: <code>{_esc(type(error).__name__)}</code> — {_esc(str(error))}"

    return f"<b>{ALERT_PREFIX}</b>\n\n{detail}"


def _format_model_section(
    models: list[ModelUsage],
    total_tokens: int,
    total_cost_usd: float,
) -> list[str]:
    if not models:
        return ["Không có usage", f"Tổng: 0 tokens | {_fmt_usd(0)}"]

    lines: list[str] = []
    for m in models:
        lines.append(
            f"• <code>{_esc(m.model)}</code>: "
            f"{_fmt_int(m.total_tokens)} tokens | {_fmt_usd(m.cost_usd)}"
        )
        detail_parts: list[str] = []
        if m.input_tokens or m.output_tokens or m.cache_write_tokens or m.cache_read_tokens:
            detail_parts.append(
                f"in {_fmt_int(m.input_tokens)} / out {_fmt_int(m.output_tokens)}"
            )
            if m.cache_write_tokens:
                detail_parts.append(f"cacheW {_fmt_int(m.cache_write_tokens)}")
            if m.cache_read_tokens:
                detail_parts.append(f"cacheR {_fmt_int(m.cache_read_tokens)}")
            detail_parts.append(f"{m.event_count} req")
            lines.append(f"  <i>{_esc(' · '.join(detail_parts))}</i>")

    lines.append(
        f"<b>Tổng:</b> {_fmt_int(total_tokens)} tokens | {_fmt_usd(total_cost_usd)}"
    )
    return lines


def _fmt_dt(dt: datetime) -> str:
    return _esc(dt.strftime("%Y-%m-%d %H:%M %Z"))


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return _fmt_int(int(value))
    return f"{value:,.2f}"


def _fmt_usd(value: float) -> str:
    abs_v = abs(value)
    if abs_v == 0:
        return "$0.00"
    if abs_v < 0.01:
        return f"${value:.4f}"
    if abs_v < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
