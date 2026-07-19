"""Aggregate usage events into yesterday + cycle-to-date report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Plan breakdown units: Pro included 2000 ↔ $20 → 1 unit = $0.01
PLAN_UNIT_USD = 0.01

# Individual monthly seat prices (USD), keyed by membershipType from usage-summary
MEMBERSHIP_PRICE_USD: dict[str, float] = {
    "hobby": 0.0,
    "free": 0.0,
    "pro": 20.0,
    "pro_plus": 60.0,
    "pro+": 60.0,
    "ultra": 200.0,
}


@dataclass(frozen=True)
class ModelUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    total_tokens: int
    cost_usd: float
    event_count: int


@dataclass(frozen=True)
class UsageReport:
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    membership_type: str
    promo_bonus: int | None
    promo_note: str
    plan_used: float | None
    plan_limit: float | None
    report_date: date
    yesterday: list[ModelUsage]
    yesterday_total_tokens: int
    yesterday_total_cost_usd: float
    cycle_to_date: list[ModelUsage]
    cycle_total_tokens: int
    cycle_total_cost_usd: float
    # Money estimates (USD)
    subscription_usd: float | None
    included_usd: float | None
    promo_bonus_usd: float | None
    on_demand_usd: float


@dataclass(frozen=True)
class ReportWindow:
    """Time windows used for fetching and filtering events."""

    timezone: str
    report_date: date
    yesterday_start: datetime
    yesterday_end: datetime
    cycle_start: datetime
    cycle_end: datetime  # end of yesterday

    @property
    def fetch_start_ms(self) -> int:
        return _to_epoch_ms(self.cycle_start)

    @property
    def fetch_end_ms(self) -> int:
        return _to_epoch_ms(self.cycle_end)


def compute_report_window(
    summary: dict[str, Any],
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> ReportWindow:
    """Compute yesterday + cycle-to-date windows from summary and timezone."""
    tz = ZoneInfo(timezone_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = current.date()
    report_date = today - timedelta(days=1)

    yesterday_start = datetime(
        report_date.year, report_date.month, report_date.day, 0, 0, 0, 0, tzinfo=tz
    )
    # Inclusive end of yesterday: 23:59:59.999
    yesterday_end = yesterday_start + timedelta(days=1) - timedelta(milliseconds=1)

    cycle_start = _parse_iso_datetime(summary["billingCycleStart"]).astimezone(tz)
    # Cycle window ends at end of yesterday (not "now")
    cycle_end = yesterday_end

    return ReportWindow(
        timezone=timezone_name,
        report_date=report_date,
        yesterday_start=yesterday_start,
        yesterday_end=yesterday_end,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
    )


def build_report(
    summary: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    timezone: str,
    now: datetime | None = None,
) -> UsageReport:
    """Build UsageReport from usage-summary + filtered events."""
    window = compute_report_window(summary, timezone_name=timezone, now=now)

    cycle_events: list[dict[str, Any]] = []
    yesterday_events: list[dict[str, Any]] = []

    for event in events:
        ts = _event_timestamp(event)
        if ts is None:
            continue
        if window.cycle_start <= ts <= window.cycle_end:
            cycle_events.append(event)
        if window.yesterday_start <= ts <= window.yesterday_end:
            yesterday_events.append(event)

    yesterday = _aggregate_by_model(yesterday_events)
    cycle_to_date = _aggregate_by_model(cycle_events)

    promo_bonus, promo_note = _extract_promo(summary)
    plan_used, plan_limit = _extract_plan_usage(summary)
    included_units, bonus_units = _extract_plan_breakdown_units(summary)

    membership_type = str(summary.get("membershipType") or "unknown")
    subscription_usd = _subscription_price_usd(membership_type)
    included_usd = (
        round(included_units * PLAN_UNIT_USD, 2)
        if included_units is not None
        else None
    )
    promo_bonus_usd = (
        round(bonus_units * PLAN_UNIT_USD, 2) if bonus_units is not None else None
    )

    cycle_total_cost_usd = round(sum(m.cost_usd for m in cycle_to_date), 6)
    free_pool = (included_usd or 0.0) + (promo_bonus_usd or 0.0)
    on_demand_api = _extract_on_demand_usd(summary)
    # Align with cycle-to-date window when we know free pool; else API / gross
    if included_usd is not None or promo_bonus_usd is not None:
        on_demand_usd = round(max(0.0, cycle_total_cost_usd - free_pool), 2)
    elif on_demand_api is not None:
        on_demand_usd = on_demand_api
    else:
        on_demand_usd = round(cycle_total_cost_usd, 2)

    billing_end = _parse_iso_datetime(summary["billingCycleEnd"]).astimezone(
        ZoneInfo(timezone)
    )

    return UsageReport(
        billing_cycle_start=window.cycle_start,
        billing_cycle_end=billing_end,
        membership_type=membership_type,
        promo_bonus=promo_bonus,
        promo_note=promo_note,
        plan_used=plan_used,
        plan_limit=plan_limit,
        report_date=window.report_date,
        yesterday=yesterday,
        yesterday_total_tokens=sum(m.total_tokens for m in yesterday),
        yesterday_total_cost_usd=round(sum(m.cost_usd for m in yesterday), 6),
        cycle_to_date=cycle_to_date,
        cycle_total_tokens=sum(m.total_tokens for m in cycle_to_date),
        cycle_total_cost_usd=cycle_total_cost_usd,
        subscription_usd=subscription_usd,
        included_usd=included_usd,
        promo_bonus_usd=promo_bonus_usd,
        on_demand_usd=on_demand_usd,
    )


def _aggregate_by_model(events: list[dict[str, Any]]) -> list[ModelUsage]:
    buckets: dict[str, dict[str, float | int | str]] = {}

    for event in events:
        model = event.get("model")
        if not model or not isinstance(model, str):
            model = "unknown"

        token_usage = event.get("tokenUsage")
        if not isinstance(token_usage, dict):
            token_usage = {}

        input_tokens = _as_int(token_usage.get("inputTokens"))
        output_tokens = _as_int(token_usage.get("outputTokens"))
        cache_write = _as_int(token_usage.get("cacheWriteTokens"))
        cache_read = _as_int(
            token_usage.get("cacheReadTokens") or token_usage.get("cacheRead")
        )
        total_tokens = input_tokens + output_tokens + cache_write + cache_read

        cents = event.get("chargedCents")
        if cents is None:
            cents = token_usage.get("totalCents")
        cost_usd = _as_float(cents) / 100.0

        bucket = buckets.setdefault(
            model,
            {
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "event_count": 0,
            },
        )
        bucket["input_tokens"] = int(bucket["input_tokens"]) + input_tokens
        bucket["output_tokens"] = int(bucket["output_tokens"]) + output_tokens
        bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + cache_write
        bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + cache_read
        bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
        bucket["cost_usd"] = float(bucket["cost_usd"]) + cost_usd
        bucket["event_count"] = int(bucket["event_count"]) + 1

    usages = [
        ModelUsage(
            model=str(b["model"]),
            input_tokens=int(b["input_tokens"]),
            output_tokens=int(b["output_tokens"]),
            cache_write_tokens=int(b["cache_write_tokens"]),
            cache_read_tokens=int(b["cache_read_tokens"]),
            total_tokens=int(b["total_tokens"]),
            cost_usd=round(float(b["cost_usd"]), 6),
            event_count=int(b["event_count"]),
        )
        for b in buckets.values()
    ]
    usages.sort(key=lambda m: (-m.cost_usd, m.model))
    return usages


def _extract_promo(summary: dict[str, Any]) -> tuple[int | None, str]:
    plan = (
        (summary.get("individualUsage") or {}).get("plan")
        if isinstance(summary.get("individualUsage"), dict)
        else None
    )
    bonus: int | None = None
    if isinstance(plan, dict):
        breakdown = plan.get("breakdown")
        if isinstance(breakdown, dict) and breakdown.get("bonus") is not None:
            try:
                bonus_val = int(breakdown["bonus"])
                if bonus_val > 0:
                    bonus = bonus_val
            except (TypeError, ValueError):
                bonus = None

    notes: list[str] = []
    if bonus is not None:
        notes.append(f"Bonus: {bonus}")

    for key in (
        "autoModelSelectedDisplayMessage",
        "namedModelSelectedDisplayMessage",
    ):
        msg = summary.get(key)
        if isinstance(msg, str) and msg.strip():
            notes.append(msg.strip())

    if not notes:
        return None, "Không có khuyến mại"
    return bonus, " | ".join(notes)


def _extract_plan_usage(summary: dict[str, Any]) -> tuple[float | None, float | None]:
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None, None
    plan = individual.get("plan")
    if not isinstance(plan, dict):
        return None, None
    used = plan.get("used")
    limit = plan.get("limit")
    return (
        _as_float(used) if used is not None else None,
        _as_float(limit) if limit is not None else None,
    )


def _extract_plan_breakdown_units(
    summary: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Return (included_units, bonus_units) from plan.breakdown."""
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None, None
    plan = individual.get("plan")
    if not isinstance(plan, dict):
        return None, None
    breakdown = plan.get("breakdown")
    if not isinstance(breakdown, dict):
        # Fallback: treat plan.limit as included units
        limit = plan.get("limit")
        if limit is None:
            return None, None
        return _as_int(limit), None

    included: int | None = None
    bonus: int | None = None
    if breakdown.get("included") is not None:
        included = _as_int(breakdown["included"])
    elif plan.get("limit") is not None:
        included = _as_int(plan["limit"])
    if breakdown.get("bonus") is not None:
        bonus_val = _as_int(breakdown["bonus"])
        bonus = bonus_val if bonus_val > 0 else None
    return included, bonus


def _extract_on_demand_usd(summary: dict[str, Any]) -> float | None:
    """On-demand spend from usage-summary (cents → USD), if present."""
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None
    on_demand = individual.get("onDemand")
    if not isinstance(on_demand, dict):
        return None
    if not on_demand.get("enabled") and on_demand.get("used") is None:
        return None
    used = on_demand.get("used")
    if used is None:
        return None
    return round(_as_float(used) / 100.0, 2)


def _subscription_price_usd(membership_type: str) -> float | None:
    key = membership_type.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MEMBERSHIP_PRICE_USD:
        return MEMBERSHIP_PRICE_USD[key]
    # e.g. "pro_plus" variants
    if "ultra" in key:
        return MEMBERSHIP_PRICE_USD["ultra"]
    if "pro_plus" in key or "proplus" in key or key == "pro+":
        return MEMBERSHIP_PRICE_USD["pro_plus"]
    if key == "pro" or key.startswith("pro_"):
        return MEMBERSHIP_PRICE_USD["pro"]
    return None


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if raw is None:
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _parse_iso_datetime(value: str | Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO datetime string, got {type(value)!r}")
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
