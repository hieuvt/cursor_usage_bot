"""Aggregate usage events into yesterday + cycle-to-date report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Individual monthly seat prices (USD), keyed by membershipType from usage-summary.
# This is "số tiền included" for the plan pool.
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
    plan_units: float
    cost_usd: float
    event_count: int


@dataclass(frozen=True)
class UsageReport:
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    membership_type: str
    report_date: date
    # Package
    included_money_usd: float | None
    included_plan_units: float | None
    usd_per_plan_unit: float | None
    bonus_plan_units: float
    # Cycle totals (aligned to end of yesterday via events)
    cycle_plan_units: float
    cycle_spent_usd: float
    included_remaining_units: float | None
    included_remaining_usd: float | None
    bonus_remaining_units: float
    on_demand_usd: float
    projected_eoc_plan_units: float | None
    estimated_on_demand_eoc_usd: float | None
    days_elapsed: int
    remaining_days: int
    # Yesterday
    yesterday_plan_units: float
    yesterday_spent_usd: float
    yesterday: list[ModelUsage]
    cycle_to_date: list[ModelUsage]


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
        return _to_epoch_ms(min(self.cycle_start, self.yesterday_start))

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
    yesterday_end = yesterday_start + timedelta(days=1) - timedelta(milliseconds=1)

    cycle_start = _parse_iso_datetime(summary["billingCycleStart"]).astimezone(tz)
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

    membership_type = str(summary.get("membershipType") or "unknown")
    included_money_usd = _subscription_price_usd(membership_type)
    included_plan_units, bonus_plan_units = _extract_pool_units(summary)
    bonus_used_units = _extract_bonus_used(summary)
    on_demand_usd = _extract_on_demand_usd(summary) or 0.0

    usd_per_plan_unit: float | None = None
    if (
        included_money_usd is not None
        and included_plan_units is not None
        and included_plan_units > 0
    ):
        usd_per_plan_unit = included_money_usd / included_plan_units

    yesterday = _aggregate_by_model(yesterday_events, usd_per_plan_unit)
    cycle_to_date = _aggregate_by_model(cycle_events, usd_per_plan_unit)

    cycle_plan_units = round(sum(m.plan_units for m in cycle_to_date), 4)
    yesterday_plan_units = round(sum(m.plan_units for m in yesterday), 4)

    cycle_spent_usd = (
        round(cycle_plan_units * usd_per_plan_unit, 2)
        if usd_per_plan_unit is not None
        else round(sum(m.cost_usd for m in cycle_to_date), 2)
    )
    yesterday_spent_usd = (
        round(yesterday_plan_units * usd_per_plan_unit, 2)
        if usd_per_plan_unit is not None
        else round(sum(m.cost_usd for m in yesterday), 2)
    )

    # Remaining included as of end of yesterday (event-based, not live summary).
    included_remaining_units: float | None = None
    included_remaining_usd: float | None = None
    if included_plan_units is not None:
        used_from_included = max(0.0, cycle_plan_units - bonus_used_units)
        included_remaining_units = round(
            max(0.0, included_plan_units - used_from_included), 4
        )
        if usd_per_plan_unit is not None:
            included_remaining_usd = round(
                included_remaining_units * usd_per_plan_unit, 2
            )

    bonus_remaining_units = round(max(0.0, bonus_plan_units - bonus_used_units), 4)

    billing_end = _parse_iso_datetime(summary["billingCycleEnd"]).astimezone(
        ZoneInfo(timezone)
    )
    days_elapsed, remaining_days = _cycle_day_counts(
        cycle_start=window.cycle_start.date(),
        report_date=window.report_date,
        cycle_end=billing_end.date(),
    )
    total_cycle_days = days_elapsed + remaining_days

    projected_eoc_plan_units: float | None = None
    estimated_on_demand_eoc_usd: float | None = None
    if days_elapsed > 0 and total_cycle_days > 0:
        avg_daily = cycle_plan_units / days_elapsed
        projected_eoc_plan_units = round(avg_daily * total_cycle_days, 4)

        total_pool = (included_plan_units or 0.0) + bonus_plan_units
        if projected_eoc_plan_units is not None and total_pool > 0:
            overage = max(0.0, projected_eoc_plan_units - total_pool)
            if usd_per_plan_unit is not None:
                estimated_on_demand_eoc_usd = round(
                    max(on_demand_usd, overage * usd_per_plan_unit), 2
                )
            else:
                estimated_on_demand_eoc_usd = round(on_demand_usd, 2)
        else:
            estimated_on_demand_eoc_usd = round(on_demand_usd, 2)

    return UsageReport(
        billing_cycle_start=window.cycle_start,
        billing_cycle_end=billing_end,
        membership_type=membership_type,
        report_date=window.report_date,
        included_money_usd=included_money_usd,
        included_plan_units=included_plan_units,
        usd_per_plan_unit=usd_per_plan_unit,
        bonus_plan_units=bonus_plan_units,
        cycle_plan_units=cycle_plan_units,
        cycle_spent_usd=cycle_spent_usd,
        included_remaining_units=included_remaining_units,
        included_remaining_usd=included_remaining_usd,
        bonus_remaining_units=bonus_remaining_units,
        on_demand_usd=on_demand_usd,
        projected_eoc_plan_units=projected_eoc_plan_units,
        estimated_on_demand_eoc_usd=estimated_on_demand_eoc_usd,
        days_elapsed=days_elapsed,
        remaining_days=remaining_days,
        yesterday_plan_units=yesterday_plan_units,
        yesterday_spent_usd=yesterday_spent_usd,
        yesterday=yesterday,
        cycle_to_date=cycle_to_date,
    )


def _cycle_day_counts(
    *,
    cycle_start: date,
    report_date: date,
    cycle_end: date,
) -> tuple[int, int]:
    days_elapsed = (report_date - cycle_start).days + 1
    if days_elapsed < 1:
        days_elapsed = 1
    remaining_days = (cycle_end - report_date).days
    if remaining_days < 0:
        remaining_days = 0
    return days_elapsed, remaining_days


def _aggregate_by_model(
    events: list[dict[str, Any]],
    usd_per_plan_unit: float | None,
) -> list[ModelUsage]:
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

        # Cursor meters plan units ≈ chargedCents (raw). Fallback to totalCents.
        cents = event.get("chargedCents")
        if cents is None:
            cents = token_usage.get("totalCents")
        plan_units = _as_float(cents)
        if usd_per_plan_unit is not None:
            cost_usd = plan_units * usd_per_plan_unit
        else:
            cost_usd = plan_units / 100.0

        bucket = buckets.setdefault(
            model,
            {
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "plan_units": 0.0,
                "cost_usd": 0.0,
                "event_count": 0,
            },
        )
        bucket["input_tokens"] = int(bucket["input_tokens"]) + input_tokens
        bucket["output_tokens"] = int(bucket["output_tokens"]) + output_tokens
        bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + cache_write
        bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + cache_read
        bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
        bucket["plan_units"] = float(bucket["plan_units"]) + plan_units
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
            plan_units=round(float(b["plan_units"]), 4),
            cost_usd=round(float(b["cost_usd"]), 6),
            event_count=int(b["event_count"]),
        )
        for b in buckets.values()
    ]
    usages.sort(key=lambda m: (-m.plan_units, m.model))
    return usages


def _extract_pool_units(summary: dict[str, Any]) -> tuple[float | None, float]:
    """Return (included_plan_units pool, bonus_plan_units pool)."""
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None, 0.0
    plan = individual.get("plan")
    if not isinstance(plan, dict):
        return None, 0.0

    limit = plan.get("limit")
    included_pool = _as_float(limit) if limit is not None else None

    # Cursor exposes bonus *used* in breakdown.bonus; a separate bonus pool size
    # is not always present. When bonus used > 0 but no pool field, treat used
    # as the known bonus allotment floor (remaining may be 0).
    bonus_pool = 0.0
    breakdown = plan.get("breakdown")
    if isinstance(breakdown, dict):
        # Prefer explicit pool fields if Cursor adds them later.
        for key in ("bonusLimit", "bonusTotal", "bonus_pool"):
            if breakdown.get(key) is not None:
                bonus_pool = _as_float(breakdown[key])
                break
        else:
            bonus_used = _as_float(breakdown.get("bonus"))
            # If limit already includes bonus, we cannot split cleanly; keep
            # included = limit and bonus pool = 0 unless bonus used implies allotment.
            if bonus_used > 0 and bonus_pool == 0.0:
                bonus_pool = bonus_used

    return included_pool, bonus_pool


def _extract_bonus_used(summary: dict[str, Any]) -> float:
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return 0.0
    plan = individual.get("plan")
    if not isinstance(plan, dict):
        return 0.0
    breakdown = plan.get("breakdown")
    if not isinstance(breakdown, dict) or breakdown.get("bonus") is None:
        return 0.0
    return max(0.0, _as_float(breakdown["bonus"]))


def _extract_on_demand_usd(summary: dict[str, Any]) -> float | None:
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
