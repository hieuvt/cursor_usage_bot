"""Aggregate usage events into yesterday + cycle-to-date report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

# Individual monthly seat prices (USD), keyed by membershipType from usage-summary.
MEMBERSHIP_PRICE_USD: dict[str, float] = {
    "hobby": 0.0,
    "free": 0.0,
    "pro": 20.0,
    "pro_plus": 60.0,
    "pro+": 60.0,
    "ultra": 200.0,
}

PoolKind = Literal["Cursor", "Other"]

# 1 plan unit = 1 chargedCent = $0.01 of API-equivalent usage.
USD_PER_PLAN_UNIT = 0.01


@dataclass(frozen=True)
class ModelUsage:
    model: str
    pool: PoolKind
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    total_tokens: int
    plan_units: float
    pool_pct: float | None
    event_count: int


@dataclass(frozen=True)
class UsageReport:
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    membership_type: str
    report_date: date
    included_money_usd: float | None
    cursor_included_units: float | None
    other_included_units: float | None
    cursor_bonus_units: float
    other_bonus_units: float
    yesterday_cursor_units: float
    yesterday_other_units: float
    yesterday_cursor_pct: float | None
    yesterday_other_pct: float | None
    cycle_cursor_units: float
    cycle_other_units: float
    cycle_cursor_pct: float | None
    cycle_other_pct: float | None
    projected_eoc_cursor_units: float | None
    projected_eoc_other_units: float | None
    estimated_on_demand_eoc_usd: float | None
    days_elapsed: int
    remaining_days: int
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


def classify_model_pool(model: str) -> PoolKind:
    """Map a model id to the Cursor Models or Other Models usage pool."""
    name = (model or "").strip().lower()
    if not name or name == "unknown":
        return "Other"
    if name in {"default", "auto"} or name.startswith("auto"):
        return "Cursor"
    if "composer" in name or "vega" in name or "grok" in name:
        return "Cursor"
    return "Other"


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
    other_included_units = _extract_other_included_units(summary)
    bonus_used = _extract_bonus_used(summary)
    on_demand_usd = _extract_on_demand_usd(summary) or 0.0

    yesterday_raw = _aggregate_by_model(yesterday_events)
    cycle_raw = _aggregate_by_model(cycle_events)

    yesterday_cursor_units = _sum_pool(yesterday_raw, "Cursor")
    yesterday_other_units = _sum_pool(yesterday_raw, "Other")
    cycle_cursor_units = _sum_pool(cycle_raw, "Cursor")
    cycle_other_units = _sum_pool(cycle_raw, "Other")

    cursor_included_units = _infer_cursor_included_units(summary, cycle_cursor_units)
    # API exposes a single bonus used figure, not a per-pool bonus grant.
    cursor_bonus_units = bonus_used
    other_bonus_units = 0.0

    yesterday_cursor_pct = _pct_of_pool(yesterday_cursor_units, cursor_included_units)
    yesterday_other_pct = _pct_of_pool(yesterday_other_units, other_included_units)
    cycle_cursor_pct = _pct_of_pool(cycle_cursor_units, cursor_included_units)
    cycle_other_pct = _pct_of_pool(cycle_other_units, other_included_units)

    yesterday = _with_pool_pct(yesterday_raw, cursor_included_units, other_included_units)
    cycle_to_date = _with_pool_pct(cycle_raw, cursor_included_units, other_included_units)

    billing_end = _parse_iso_datetime(summary["billingCycleEnd"]).astimezone(
        ZoneInfo(timezone)
    )
    days_elapsed, remaining_days = _cycle_day_counts(
        cycle_start=window.cycle_start.date(),
        report_date=window.report_date,
        cycle_end=billing_end.date(),
    )
    total_cycle_days = days_elapsed + remaining_days

    projected_eoc_cursor_units: float | None = None
    projected_eoc_other_units: float | None = None
    estimated_on_demand_eoc_usd: float | None = None
    if days_elapsed > 0 and total_cycle_days > 0:
        projected_eoc_cursor_units = round(
            (cycle_cursor_units / days_elapsed) * total_cycle_days, 4
        )
        projected_eoc_other_units = round(
            (cycle_other_units / days_elapsed) * total_cycle_days, 4
        )
        estimated_on_demand_eoc_usd = _estimate_on_demand_eoc(
            projected_cursor=projected_eoc_cursor_units,
            projected_other=projected_eoc_other_units,
            cursor_included=cursor_included_units,
            other_included=other_included_units,
            cursor_bonus=cursor_bonus_units,
            other_bonus=other_bonus_units,
            on_demand_already_usd=on_demand_usd,
        )

    return UsageReport(
        billing_cycle_start=window.cycle_start,
        billing_cycle_end=billing_end,
        membership_type=membership_type,
        report_date=window.report_date,
        included_money_usd=included_money_usd,
        cursor_included_units=cursor_included_units,
        other_included_units=other_included_units,
        cursor_bonus_units=cursor_bonus_units,
        other_bonus_units=other_bonus_units,
        yesterday_cursor_units=yesterday_cursor_units,
        yesterday_other_units=yesterday_other_units,
        yesterday_cursor_pct=yesterday_cursor_pct,
        yesterday_other_pct=yesterday_other_pct,
        cycle_cursor_units=cycle_cursor_units,
        cycle_other_units=cycle_other_units,
        cycle_cursor_pct=cycle_cursor_pct,
        cycle_other_pct=cycle_other_pct,
        projected_eoc_cursor_units=projected_eoc_cursor_units,
        projected_eoc_other_units=projected_eoc_other_units,
        estimated_on_demand_eoc_usd=estimated_on_demand_eoc_usd,
        days_elapsed=days_elapsed,
        remaining_days=remaining_days,
        yesterday=yesterday,
        cycle_to_date=cycle_to_date,
    )


def _cycle_day_counts(
    *,
    cycle_start: date,
    report_date: date,
    cycle_end: date,
) -> tuple[int, int]:
    """Count calendar days in the cycle, excluding the reset date.

    ``cycle_end`` is Cursor's billingCycleEnd (the instant the next cycle
    starts). The last full day of *this* cycle is the day before that date,
    through 23:59. Start date stays inclusive — usage on signup day is real.
    """
    last_full_day = cycle_end - timedelta(days=1)
    if last_full_day < cycle_start:
        last_full_day = cycle_start

    clamped = report_date
    if clamped < cycle_start:
        clamped = cycle_start
    if clamped > last_full_day:
        clamped = last_full_day

    days_elapsed = (clamped - cycle_start).days + 1
    remaining_days = (last_full_day - clamped).days
    return days_elapsed, remaining_days


def _aggregate_by_model(events: list[dict[str, Any]]) -> list[ModelUsage]:
    buckets: dict[str, dict[str, float | int | str]] = {}

    for event in events:
        model = event.get("model")
        if not model or not isinstance(model, str):
            model = "unknown"
        pool = classify_model_pool(model)

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
        plan_units = _as_float(cents)

        bucket = buckets.setdefault(
            model,
            {
                "model": model,
                "pool": pool,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "plan_units": 0.0,
                "event_count": 0,
            },
        )
        bucket["input_tokens"] = int(bucket["input_tokens"]) + input_tokens
        bucket["output_tokens"] = int(bucket["output_tokens"]) + output_tokens
        bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + cache_write
        bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + cache_read
        bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
        bucket["plan_units"] = float(bucket["plan_units"]) + plan_units
        bucket["event_count"] = int(bucket["event_count"]) + 1

    usages = [
        ModelUsage(
            model=str(b["model"]),
            pool=b["pool"] if b["pool"] in ("Cursor", "Other") else "Other",
            input_tokens=int(b["input_tokens"]),
            output_tokens=int(b["output_tokens"]),
            cache_write_tokens=int(b["cache_write_tokens"]),
            cache_read_tokens=int(b["cache_read_tokens"]),
            total_tokens=int(b["total_tokens"]),
            plan_units=round(float(b["plan_units"]), 4),
            pool_pct=None,
            event_count=int(b["event_count"]),
        )
        for b in buckets.values()
    ]
    usages.sort(key=lambda m: (-m.plan_units, m.model))
    return usages


def _with_pool_pct(
    models: list[ModelUsage],
    cursor_included: float | None,
    other_included: float | None,
) -> list[ModelUsage]:
    result: list[ModelUsage] = []
    for m in models:
        pool_size = cursor_included if m.pool == "Cursor" else other_included
        result.append(
            ModelUsage(
                model=m.model,
                pool=m.pool,
                input_tokens=m.input_tokens,
                output_tokens=m.output_tokens,
                cache_write_tokens=m.cache_write_tokens,
                cache_read_tokens=m.cache_read_tokens,
                total_tokens=m.total_tokens,
                plan_units=m.plan_units,
                pool_pct=_pct_of_pool(m.plan_units, pool_size),
                event_count=m.event_count,
            )
        )
    return result


def _sum_pool(models: list[ModelUsage], pool: PoolKind) -> float:
    return round(sum(m.plan_units for m in models if m.pool == pool), 4)


def _pct_of_pool(used: float, included: float | None) -> float | None:
    if included is None or included <= 0:
        return None
    return round(used / included * 100.0, 4)


def _infer_cursor_included_units(
    summary: dict[str, Any], cycle_cursor_units: float
) -> float | None:
    """Infer Cursor Models included pool from autoPercentUsed + event usage."""
    plan = _plan_dict(summary)
    if plan is None:
        return None
    auto_pct = plan.get("autoPercentUsed")
    if auto_pct is None:
        return None
    pct = _as_float(auto_pct)
    if pct <= 0 or cycle_cursor_units <= 0:
        return None
    return round(cycle_cursor_units / (pct / 100.0), 4)


def _extract_other_included_units(summary: dict[str, Any]) -> float | None:
    """Other Models included pool: plan.limit (API cents, e.g. Ultra 40000 = $400)."""
    plan = _plan_dict(summary)
    if plan is None:
        return None
    limit = plan.get("limit")
    if limit is None:
        return None
    return _as_float(limit)


def _extract_bonus_used(summary: dict[str, Any]) -> float:
    plan = _plan_dict(summary)
    if plan is None:
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


def _estimate_on_demand_eoc(
    *,
    projected_cursor: float,
    projected_other: float,
    cursor_included: float | None,
    other_included: float | None,
    cursor_bonus: float,
    other_bonus: float,
    on_demand_already_usd: float,
) -> float:
    """USD on-demand if each pool's run-rate continues; Cursor overage may spill into Other."""
    cursor_budget = (cursor_included or 0.0) + cursor_bonus
    other_budget = (other_included or 0.0) + other_bonus

    cursor_over = max(0.0, projected_cursor - cursor_budget) if cursor_budget > 0 else 0.0
    other_over = max(0.0, projected_other - other_budget) if other_budget > 0 else 0.0
    other_left = max(0.0, other_budget - projected_other) if other_budget > 0 else 0.0
    spilled = min(cursor_over, other_left)
    overage_units = cursor_over - spilled + other_over
    projected_usd = round(overage_units * USD_PER_PLAN_UNIT, 2)
    return round(max(on_demand_already_usd, projected_usd), 2)


def _plan_dict(summary: dict[str, Any]) -> dict[str, Any] | None:
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None
    plan = individual.get("plan")
    if not isinstance(plan, dict):
        return None
    return plan


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
