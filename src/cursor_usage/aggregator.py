"""Aggregate usage events into yesterday + cycle-to-date report."""

from __future__ import annotations

from dataclasses import dataclass, replace
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

PoolKind = Literal["Cursor", "Other", "On-demand"]

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
    yesterday_on_demand_units: float
    yesterday_cursor_pct: float | None
    yesterday_other_pct: float | None
    yesterday_on_demand_pct: float | None
    cycle_cursor_units: float
    cycle_other_units: float
    cycle_on_demand_units: float
    cycle_cursor_pct: float | None
    cycle_other_pct: float | None
    cycle_on_demand_pct: float | None
    projected_eoc_cursor_units: float | None
    projected_eoc_other_units: float | None
    projected_eoc_on_demand_units: float | None
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


def classify_usage_pool(model: str, tier: int | None) -> PoolKind:
    """Match the dashboard split of aggregated usage rows.

    Cursor Models: tier 2, or Auto/``default`` (including tier-1 spillover).
    Other Models: every other tier-1 row. The same model can appear in both
    pools once Cursor Models is full and further usage spills over.
    """
    name = (model or "").strip()
    if tier == 2 or (tier is None and name == "default"):
        return "Cursor"
    if name == "default" and tier == 1:
        return "Cursor"
    return "Other"


def classify_model_pool(model: str) -> PoolKind:
    """Map a model id to a pool when no dashboard tier is available.

    Named Cursor models (Grok, Composer, Auto) start in the Cursor pool.
    Aggregated rows with a tier must use ``classify_usage_pool`` instead:
    spillover keeps the same model name and moves it to Other Models.
    """
    name = (model or "").strip().lower()
    if not name or name == "unknown":
        return "Other"
    if name in {"default", "auto"} or name.startswith("auto"):
        return "Cursor"
    if "composer" in name or "vega" in name or "grok" in name:
        return "Cursor"
    return "Other"


def infer_pool_grants(period_usage: dict[str, Any]) -> tuple[float | None, float | None]:
    """Infer Cursor and Other included grants from the dashboard percent meters.

    ``totalSpend / totalPercentUsed`` is the combined grant. While Cursor Models
    is at 100%, the unused share of that grant is the Other Models pool.
    Grants come back rounded to the nearest 100 plan units.
    """
    plan = _plan_usage(period_usage)
    if plan is None:
        return None, None

    total_spend = _as_float(plan.get("totalSpend"))
    total_pct = _as_float(plan.get("totalPercentUsed"))
    auto_pct = _as_float(plan.get("autoPercentUsed"))
    api_pct = _as_float(plan.get("apiPercentUsed"))
    if total_spend <= 0 or total_pct <= 0:
        return None, None

    total_grant = round(total_spend / (total_pct / 100.0))
    auto_frac = auto_pct / 100.0
    api_frac = api_pct / 100.0

    if auto_pct >= 100.0:
        if api_frac >= 1.0:
            return None, None
        raw_other = (total_grant - total_spend) / (1.0 - api_frac)
    elif api_pct >= 100.0:
        if auto_frac >= 1.0:
            return None, None
        raw_cursor = (total_grant - total_spend) / (1.0 - auto_frac)
        raw_other = total_grant - raw_cursor
    else:
        denom = auto_frac - api_frac
        if abs(denom) < 1e-9:
            return None, None
        raw_cursor = (total_spend - api_frac * total_grant) / denom
        raw_other = total_grant - raw_cursor

    other_grant = int(round(raw_other / 100.0) * 100)
    cursor_grant = int(total_grant - other_grant)
    if cursor_grant < 0 or other_grant < 0:
        return None, None
    return float(cursor_grant), float(other_grant)


def build_report(
    summary: dict[str, Any],
    *,
    period_usage: dict[str, Any],
    yesterday_aggregations: list[dict[str, Any]],
    cycle_aggregations: list[dict[str, Any]],
    projection_aggregations: list[dict[str, Any]],
    timezone: str,
    now: datetime | None = None,
    usage_events: list[dict[str, Any]] | None = None,
) -> UsageReport:
    """Build a report aligned with the dashboard Spending pools.

    Cycle percents are ``autoPercentUsed`` / ``apiPercentUsed`` (the Spending
    bars). Model rows come from aggregated usage split by tier, for the current
    period. Yesterday is the previous local day. The end-of-cycle projection
    uses full days only (through yesterday): Cursor stops at its grant, the
    overflow is added to Other, and only usage past both grants is on-demand.
    """
    window = compute_report_window(summary, timezone_name=timezone, now=now)
    plan = _plan_usage(period_usage) or {}

    membership_type = str(summary.get("membershipType") or "unknown")
    included_money_usd = _subscription_price_usd(membership_type)
    on_demand_usd = _extract_on_demand_usd(summary) or 0.0
    on_demand_limit = _extract_on_demand_limit(summary)
    cycle_on_demand_units = _extract_on_demand_units(summary)

    cursor_included_units, other_included_units = infer_pool_grants(period_usage)
    # Pool grants already include purchased usage and provider bonus.
    # bonusSpend is consumption, not a second allowance on top of the pools.
    cursor_bonus_units = 0.0
    other_bonus_units = 0.0

    events = usage_events or []
    yesterday_on_demand = _on_demand_models(
        events, start=window.yesterday_start, end=window.yesterday_end
    )
    cycle_on_demand_rows = _on_demand_models(
        events, start=window.cycle_start, end=window.cycle_end
    )
    # Live cycle rows run through now, so on-demand after yesterday still counts.
    live_end = now.astimezone(ZoneInfo(timezone)) if now is not None else window.cycle_end
    if live_end > window.cycle_end:
        cycle_on_demand_rows = _on_demand_models(
            events, start=window.cycle_start, end=live_end
        )

    yesterday_raw = _without_on_demand(
        _models_from_aggregations(yesterday_aggregations), yesterday_on_demand
    )
    cycle_raw = _without_on_demand(
        _models_from_aggregations(cycle_aggregations), cycle_on_demand_rows
    )
    projection_raw = _models_from_aggregations(projection_aggregations)

    yesterday_cursor_units = _sum_pool(yesterday_raw, "Cursor")
    yesterday_other_units = _sum_pool(yesterday_raw, "Other")
    yesterday_on_demand_units = _sum_pool(yesterday_on_demand, "On-demand")
    cycle_cursor_units = _sum_pool(cycle_raw, "Cursor")
    cycle_other_units = _sum_pool(cycle_raw, "Other")
    projection_cursor_units = _sum_pool(projection_raw, "Cursor")
    projection_other_units = _sum_pool(projection_raw, "Other")

    yesterday_cursor_pct = _pct_of_pool(yesterday_cursor_units, cursor_included_units)
    yesterday_other_pct = _pct_of_pool(yesterday_other_units, other_included_units)
    yesterday_on_demand_pct = _pct_of_pool(yesterday_on_demand_units, on_demand_limit)
    cycle_cursor_pct = _dashboard_percent(plan.get("autoPercentUsed"))
    cycle_other_pct = _dashboard_percent(plan.get("apiPercentUsed"))
    cycle_on_demand_pct = _pct_of_pool(cycle_on_demand_units, on_demand_limit)

    yesterday = _with_on_demand_rows(
        _with_share_of_percent(yesterday_raw, yesterday_cursor_pct, yesterday_other_pct),
        yesterday_on_demand,
        on_demand_limit,
    )
    cycle_to_date = _with_on_demand_rows(
        _with_share_of_percent(cycle_raw, cycle_cursor_pct, cycle_other_pct),
        cycle_on_demand_rows,
        on_demand_limit,
    )

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
    projected_eoc_on_demand_units: float | None = None
    estimated_on_demand_eoc_usd: float | None = None
    if days_elapsed > 0 and total_cycle_days > 0:
        (
            projected_eoc_cursor_units,
            projected_eoc_other_units,
            projected_eoc_on_demand_units,
            estimated_on_demand_eoc_usd,
        ) = project_end_of_cycle(
            cursor_used=projection_cursor_units,
            other_used=projection_other_units,
            days_elapsed=days_elapsed,
            total_cycle_days=total_cycle_days,
            cursor_included=cursor_included_units,
            other_included=other_included_units,
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
        yesterday_on_demand_units=yesterday_on_demand_units,
        yesterday_cursor_pct=yesterday_cursor_pct,
        yesterday_other_pct=yesterday_other_pct,
        yesterday_on_demand_pct=yesterday_on_demand_pct,
        cycle_cursor_units=cycle_cursor_units,
        cycle_other_units=cycle_other_units,
        cycle_on_demand_units=cycle_on_demand_units,
        cycle_cursor_pct=cycle_cursor_pct,
        cycle_other_pct=cycle_other_pct,
        cycle_on_demand_pct=cycle_on_demand_pct,
        projected_eoc_cursor_units=projected_eoc_cursor_units,
        projected_eoc_other_units=projected_eoc_other_units,
        projected_eoc_on_demand_units=projected_eoc_on_demand_units,
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


def _plan_usage(period_usage: dict[str, Any]) -> dict[str, Any] | None:
    plan = period_usage.get("planUsage")
    if isinstance(plan, dict):
        return plan
    if "autoPercentUsed" in period_usage and "totalSpend" in period_usage:
        return period_usage
    return None


def _dashboard_percent(value: Any) -> float | None:
    """Percent as shown on the Spending bar (JavaScript Math.round)."""
    if value is None:
        return None
    pct = _as_float(value)
    if pct >= 0:
        return float(int(pct + 0.5))
    return float(int(pct - 0.5))


def _models_from_aggregations(rows: list[dict[str, Any]]) -> list[ModelUsage]:
    """Sum aggregated rows into model + pool buckets, using the dashboard tier."""
    buckets: dict[tuple[str, str], dict[str, float | int | str]] = {}

    for row in rows:
        model = row.get("modelIntent")
        if not model or not isinstance(model, str):
            model = row.get("model")
        if not model or not isinstance(model, str):
            model = "unknown"

        tier = _optional_int(row.get("tier"))
        pool = classify_usage_pool(model, tier)
        total_tokens = (
            _as_int(row.get("inputTokens"))
            + _as_int(row.get("outputTokens"))
            + _as_int(row.get("cacheWriteTokens"))
            + _as_int(row.get("cacheReadTokens"))
        )
        plan_units = _as_float(row.get("totalCents"))
        if total_tokens == 0 and plan_units == 0:
            continue

        key = (model, pool)
        bucket = buckets.setdefault(
            key,
            {
                "model": model,
                "pool": pool,
                "total_tokens": 0,
                "plan_units": 0.0,
                "event_count": 0,
            },
        )
        bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
        bucket["plan_units"] = float(bucket["plan_units"]) + plan_units
        bucket["event_count"] = int(bucket["event_count"]) + 1

    usages = [
        ModelUsage(
            model=str(b["model"]),
            pool=b["pool"] if b["pool"] in ("Cursor", "Other") else "Other",
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=0,
            total_tokens=int(b["total_tokens"]),
            plan_units=round(float(b["plan_units"]), 4),
            pool_pct=None,
            event_count=int(b["event_count"]),
        )
        for b in buckets.values()
    ]
    usages.sort(key=lambda m: (-m.plan_units, m.model))
    return usages


def _with_share_of_percent(
    models: list[ModelUsage],
    cursor_pct: float | None,
    other_pct: float | None,
) -> list[ModelUsage]:
    """Attribute each model's share of its pool's displayed percent."""
    totals = {
        "Cursor": sum(m.plan_units for m in models if m.pool == "Cursor"),
        "Other": sum(m.plan_units for m in models if m.pool == "Other"),
    }
    result: list[ModelUsage] = []
    for m in models:
        displayed = cursor_pct if m.pool == "Cursor" else other_pct
        total = totals[m.pool]
        share = None
        if displayed is not None and total > 0:
            share = round(m.plan_units / total * displayed, 4)
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
                pool_pct=share,
                event_count=m.event_count,
            )
        )
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_pool(models: list[ModelUsage], pool: PoolKind) -> float:
    return round(sum(m.plan_units for m in models if m.pool == pool), 4)


def _pct_of_pool(used: float, included: float | None) -> float | None:
    if included is None or included <= 0:
        return None
    return round(used / included * 100.0, 4)


def _is_on_demand_event(event: dict[str, Any]) -> bool:
    """Usage-based charges are on-demand. Included and free stays in the pools."""
    kind = str(event.get("kind") or "").upper()
    if not kind or "INCLUDED" in kind or "FREE" in kind:
        return False
    return "USAGE_BASED" in kind or "ON_DEMAND" in kind or "ONDEMAND" in kind


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if raw is None or raw == "":
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _on_demand_models(
    events: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> list[ModelUsage]:
    """Sum usage-based events in ``[start, end]`` into On-demand model rows."""
    buckets: dict[str, dict[str, float | int | str]] = {}
    for event in events:
        if not _is_on_demand_event(event):
            continue
        moment = _event_timestamp(event)
        if moment is None or moment < start or moment > end:
            continue
        model = event.get("model")
        if not isinstance(model, str) or not model.strip():
            model = "unknown"
        tokens = event.get("tokenUsage") if isinstance(event.get("tokenUsage"), dict) else {}
        total_tokens = (
            _as_int(tokens.get("inputTokens"))
            + _as_int(tokens.get("outputTokens"))
            + _as_int(tokens.get("cacheWriteTokens"))
            + _as_int(tokens.get("cacheReadTokens"))
        )
        bucket = buckets.setdefault(
            model,
            {"model": model, "total_tokens": 0, "plan_units": 0.0, "event_count": 0},
        )
        bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
        bucket["plan_units"] = float(bucket["plan_units"]) + _as_float(event.get("chargedCents"))
        bucket["event_count"] = int(bucket["event_count"]) + 1

    rows = [
        ModelUsage(
            model=str(b["model"]),
            pool="On-demand",
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=0,
            total_tokens=int(b["total_tokens"]),
            plan_units=round(float(b["plan_units"]), 4),
            pool_pct=None,
            event_count=int(b["event_count"]),
        )
        for b in buckets.values()
        if int(b["total_tokens"]) > 0 or float(b["plan_units"]) > 0
    ]
    rows.sort(key=lambda m: (-m.plan_units, m.model))
    return rows


def _without_on_demand(
    included: list[ModelUsage],
    on_demand: list[ModelUsage],
) -> list[ModelUsage]:
    """Remove usage-based cents from the tier buckets so they are not counted twice.

    Aggregated rows do not split on-demand. Those cents land in Other (then
    Cursor if Other cannot absorb them).
    """
    remaining = {(row.model, row.pool): row for row in included}
    for demand in on_demand:
        leftover_units = demand.plan_units
        leftover_tokens = demand.total_tokens
        for pool in ("Other", "Cursor"):
            key = (demand.model, pool)
            row = remaining.get(key)
            if row is None or (leftover_units <= 0 and leftover_tokens <= 0):
                continue
            take_units = min(row.plan_units, leftover_units)
            take_tokens = min(row.total_tokens, leftover_tokens)
            leftover_units = round(leftover_units - take_units, 4)
            leftover_tokens -= take_tokens
            new_units = round(row.plan_units - take_units, 4)
            new_tokens = row.total_tokens - take_tokens
            if new_units <= 0 and new_tokens <= 0:
                del remaining[key]
            else:
                remaining[key] = replace(
                    row, plan_units=new_units, total_tokens=new_tokens
                )
    rows = list(remaining.values())
    rows.sort(key=lambda m: (-m.plan_units, m.model))
    return rows


def _with_on_demand_rows(
    included: list[ModelUsage],
    on_demand: list[ModelUsage],
    limit: float | None,
) -> list[ModelUsage]:
    """Append On-demand rows. An empty table still shows one zero row."""
    priced = [_price_on_demand(row, limit) for row in on_demand]
    if not priced:
        priced = [
            ModelUsage(
                model="—",
                pool="On-demand",
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                total_tokens=0,
                plan_units=0.0,
                pool_pct=0.0,
                event_count=0,
            )
        ]
    combined = included + priced
    combined.sort(key=lambda m: (-m.plan_units, m.pool == "On-demand", m.model))
    return combined


def _price_on_demand(row: ModelUsage, limit: float | None) -> ModelUsage:
    if limit is not None and limit > 0:
        pct: float | None = round(row.plan_units / limit * 100.0, 4)
    elif row.plan_units == 0:
        pct = 0.0
    else:
        pct = None
    return replace(row, pool_pct=pct)


def _extract_on_demand_limit(summary: dict[str, Any]) -> float | None:
    on_demand = _on_demand_block(summary)
    if on_demand is None or on_demand.get("limit") is None:
        return None
    limit = _as_float(on_demand["limit"])
    if limit <= 0:
        return None
    return limit


def _extract_on_demand_units(summary: dict[str, Any]) -> float:
    """``onDemand.used`` is already in cents, which are plan units."""
    on_demand = _on_demand_block(summary)
    if on_demand is None or on_demand.get("used") is None:
        return 0.0
    return round(_as_float(on_demand["used"]), 4)


def _on_demand_block(summary: dict[str, Any]) -> dict[str, Any] | None:
    individual = summary.get("individualUsage")
    if not isinstance(individual, dict):
        return None
    on_demand = individual.get("onDemand")
    if not isinstance(on_demand, dict):
        return None
    return on_demand


def _extract_on_demand_usd(summary: dict[str, Any]) -> float | None:
    on_demand = _on_demand_block(summary)
    if on_demand is None:
        return None
    if not on_demand.get("enabled") and on_demand.get("used") is None:
        return None
    used = on_demand.get("used")
    if used is None:
        return None
    return round(_as_float(used) / 100.0, 2)


def project_end_of_cycle(
    *,
    cursor_used: float,
    other_used: float,
    days_elapsed: int,
    total_cycle_days: int,
    cursor_included: float | None,
    other_included: float | None,
    on_demand_already_usd: float,
) -> tuple[float, float, float, float]:
    """Project included-pool usage through the reset.

    Continue the combined daily rate. Cursor Models stops at its grant; every
    unit past that grant is Other Models usage. Other Models stops at its
    grant; the remainder is on-demand.
    """
    scale = total_cycle_days / days_elapsed
    raw_cursor = cursor_used * scale
    raw_other = other_used * scale

    if cursor_included is not None and cursor_included > 0:
        projected_cursor = min(raw_cursor, cursor_included)
        spill = max(0.0, raw_cursor - cursor_included)
    else:
        projected_cursor = raw_cursor
        spill = 0.0

    other_demand = raw_other + spill
    if other_included is not None and other_included > 0:
        projected_other = min(other_demand, other_included)
        on_demand_units = max(0.0, other_demand - other_included)
    else:
        projected_other = other_demand
        on_demand_units = 0.0

    on_demand_units = round(on_demand_units, 4)
    on_demand_usd = round(on_demand_units * USD_PER_PLAN_UNIT, 2)
    return (
        round(projected_cursor, 4),
        round(projected_other, 4),
        on_demand_units,
        round(max(on_demand_already_usd, on_demand_usd), 2),
    )


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
