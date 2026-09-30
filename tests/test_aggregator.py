"""Dashboard pool split: tier spillover, grants, and Spending percents."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cursor_usage.aggregator import build_report, classify_usage_pool, infer_pool_grants

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

SUMMARY = {
    "billingCycleStart": "2026-09-04T02:26:46.000Z",
    "billingCycleEnd": "2026-10-04T02:26:46.000Z",
    "membershipType": "pro_plus",
    "individualUsage": {"onDemand": {"enabled": True, "used": 0, "limit": 20000}},
}

PERIOD = {
    "planUsage": {
        "totalSpend": 124092,
        "limit": 7000,
        "bonusSpend": 117092,
        "autoPercentUsed": 100,
        "apiPercentUsed": 37.18181818181818,
        "totalPercentUsed": 94.7267175572519,
    }
}

YESTERDAY = [
    {
        "modelIntent": "grok-4.7-high",
        "tier": 1,
        "totalCents": 1557.7068,
        "inputTokens": "1",
        "outputTokens": "1",
        "cacheReadTokens": "20442566",
    },
    {
        "modelIntent": "cursor-grok-4.6-high",
        "tier": 1,
        "totalCents": 699.119,
        "inputTokens": "1",
        "outputTokens": "1",
        "cacheReadTokens": "11205325",
    },
    {"modelIntent": "gpt-5.4-mini", "tier": 1},
]

# Cursor pool is tier 2 and has stopped growing; new usage is tier 1.
CYCLE = [
    {
        "modelIntent": "cursor-grok-4.6-high",
        "tier": 2,
        "totalCents": 96720.959,
        "inputTokens": "1000",
        "outputTokens": "0",
        "cacheReadTokens": "0",
    },
    {
        "modelIntent": "grok-4.7-high",
        "tier": 1,
        "totalCents": 3187.16,
        "inputTokens": "50",
        "outputTokens": "0",
        "cacheReadTokens": "0",
    },
    {
        "modelIntent": "cursor-grok-4.6-high",
        "tier": 1,
        "totalCents": 745.28,
        "inputTokens": "10",
        "outputTokens": "0",
        "cacheReadTokens": "0",
    },
]

PROJECTION = [
    {
        "modelIntent": "cursor-grok-4.6-high",
        "tier": 2,
        "totalCents": 119722.97,
        "inputTokens": "1",
    },
    {
        "modelIntent": "grok-4.7-high",
        "tier": 1,
        "totalCents": 3530.35,
        "inputTokens": "1",
    },
]


class PoolTests(unittest.TestCase):
    def test_tier_splits_the_same_model(self) -> None:
        self.assertEqual(classify_usage_pool("grok-4.7-high", 2), "Cursor")
        self.assertEqual(classify_usage_pool("grok-4.7-high", 1), "Other")
        self.assertEqual(classify_usage_pool("default", 1), "Cursor")

    def test_grants_match_spending_pools(self) -> None:
        cursor, other = infer_pool_grants(PERIOD)
        self.assertEqual(cursor, 120_000)
        self.assertEqual(other, 11_000)

    def test_report_matches_dashboard_pools(self) -> None:
        report = build_report(
            SUMMARY,
            period_usage=PERIOD,
            yesterday_aggregations=YESTERDAY,
            cycle_aggregations=CYCLE,
            projection_aggregations=PROJECTION,
            timezone="Asia/Ho_Chi_Minh",
            now=NOW,
        )

        self.assertEqual(report.cursor_included_units, 120_000)
        self.assertEqual(report.other_included_units, 11_000)
        self.assertEqual(report.cursor_bonus_units, 0)
        self.assertEqual(report.other_bonus_units, 0)

        self.assertEqual(report.yesterday_cursor_units, 0)
        self.assertAlmostEqual(report.yesterday_other_units, 2256.8258, places=2)
        self.assertEqual(report.yesterday_cursor_pct, 0)
        self.assertAlmostEqual(report.yesterday_other_pct or 0, 20.5166, places=2)
        self.assertTrue(
            all(row.pool == "Other" for row in report.yesterday if row.pool != "On-demand")
        )

        self.assertEqual(report.cycle_cursor_pct, 100)
        self.assertEqual(report.cycle_other_pct, 37)
        pools = {(row.model, row.pool) for row in report.cycle_to_date}
        self.assertIn(("cursor-grok-4.6-high", "Cursor"), pools)
        self.assertIn(("cursor-grok-4.6-high", "Other"), pools)
        self.assertIn(("grok-4.7-high", "Other"), pools)

        # 123,253.32 used over 24 days, extrapolated to 30 = 154,066.65.
        # Cursor takes 120,000, Other takes 11,000, on-demand is the rest.
        self.assertEqual(report.projected_eoc_total_units, 154_066.65)
        self.assertEqual(report.projected_eoc_cursor_units, 120_000)
        self.assertEqual(report.projected_eoc_other_units, 11_000)
        self.assertEqual(report.yesterday_on_demand_units, 0)
        self.assertEqual(report.cycle_on_demand_units, 0)
        self.assertEqual(report.yesterday_on_demand_pct, 0)
        self.assertEqual(report.cycle_on_demand_pct, 0)
        self.assertEqual(report.projected_eoc_on_demand_units, 23_066.65)
        self.assertEqual(report.estimated_on_demand_eoc_usd, 230.67)
        for rows in (report.yesterday, report.cycle_to_date):
            blank = [row for row in rows if row.pool == "On-demand"]
            self.assertEqual(len(blank), 1)
            self.assertEqual(blank[0].model, "—")
            self.assertEqual(blank[0].plan_units, 0)
            self.assertEqual(blank[0].pool_pct, 0)

    def test_usage_based_events_are_on_demand_not_other(self) -> None:
        yesterday_ms = int(
            datetime(2026, 9, 27, 12, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")).timestamp()
            * 1000
        )
        report = build_report(
            SUMMARY,
            period_usage=PERIOD,
            yesterday_aggregations=YESTERDAY,
            cycle_aggregations=CYCLE,
            projection_aggregations=PROJECTION,
            timezone="Asia/Ho_Chi_Minh",
            now=NOW,
            usage_events=[
                {
                    "timestamp": str(yesterday_ms),
                    "model": "grok-4.7-high",
                    "kind": "USAGE_EVENT_KIND_USAGE_BASED",
                    "chargedCents": 100,
                    "tokenUsage": {
                        "inputTokens": 10,
                        "outputTokens": 0,
                        "cacheWriteTokens": 0,
                        "cacheReadTokens": 90,
                    },
                }
            ],
        )

        self.assertEqual(report.yesterday_on_demand_units, 100)
        self.assertAlmostEqual(report.yesterday_on_demand_pct or 0, 0.5, places=2)
        self.assertAlmostEqual(report.yesterday_other_units, 2256.8258, places=2)
        on_demand = [row for row in report.yesterday if row.pool == "On-demand"]
        self.assertEqual([(row.model, row.plan_units) for row in on_demand], [("grok-4.7-high", 100)])
        other = next(row for row in report.yesterday if row.model == "grok-4.7-high" and row.pool == "Other")
        self.assertAlmostEqual(other.plan_units, 1557.7068, places=2)
        self.assertEqual(report.cycle_on_demand_units, 0)

    def test_full_pools_still_cap_the_projection(self) -> None:
        # Both Spending bars are at 100%, so the unused-share formula has no
        # remainder. Grants stay 120,000 and 11,000. Overflow is on-demand.
        period = {
            "planUsage": {
                "totalSpend": 131018,
                "autoPercentUsed": 100,
                "apiPercentUsed": 100,
                "totalPercentUsed": 100,
            }
        }
        filled = [
            {
                "modelIntent": "cursor-grok-4.6-high",
                "tier": 2,
                "totalCents": 119722.97,
                "inputTokens": "1",
            },
            {
                "modelIntent": "grok-4.7-high",
                "tier": 1,
                "totalCents": 11015.78,
                "inputTokens": "1",
            },
        ]
        now = datetime(2026, 9, 29, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        report = build_report(
            SUMMARY,
            period_usage=period,
            yesterday_aggregations=[],
            cycle_aggregations=filled,
            projection_aggregations=filled,
            timezone="Asia/Ho_Chi_Minh",
            now=now,
        )

        self.assertEqual(report.days_elapsed, 25)
        self.assertEqual(report.remaining_days, 5)
        self.assertEqual(report.cursor_included_units, 120_000)
        self.assertEqual(report.other_included_units, 11_000)
        self.assertEqual(report.projected_eoc_total_units, 156_886.5)
        self.assertEqual(report.projected_eoc_cursor_units, 120_000)
        self.assertEqual(report.projected_eoc_other_units, 11_000)
        self.assertEqual(report.projected_eoc_on_demand_units, 25_886.5)
        self.assertEqual(report.estimated_on_demand_eoc_usd, 258.87)

    def test_forecast_allocates_one_total_including_on_demand(self) -> None:
        yesterday_ms = int(
            datetime(2026, 9, 28, 12, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")).timestamp()
            * 1000
        )
        period = {
            "planUsage": {
                "totalSpend": 131018,
                "autoPercentUsed": 100,
                "apiPercentUsed": 100,
                "totalPercentUsed": 100,
            }
        }
        filled = [
            {
                "modelIntent": "cursor-grok-4.6-high",
                "tier": 2,
                "totalCents": 120_000,
                "inputTokens": "1",
            },
            {
                "modelIntent": "grok-4.7-high",
                "tier": 1,
                "totalCents": 11_000,
                "inputTokens": "1",
            },
        ]
        now = datetime(2026, 9, 29, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        report = build_report(
            SUMMARY,
            period_usage=period,
            yesterday_aggregations=[],
            cycle_aggregations=filled,
            projection_aggregations=filled,
            timezone="Asia/Ho_Chi_Minh",
            now=now,
            usage_events=[
                {
                    "timestamp": str(yesterday_ms),
                    "model": "grok-4.7-high",
                    "kind": "USAGE_EVENT_KIND_USAGE_BASED",
                    "chargedCents": 1000,
                    "tokenUsage": {"inputTokens": 1, "outputTokens": 0},
                }
            ],
        )

        # (120,000 + 11,000 + 1,000) * 30 / 25 = 158,400.
        self.assertEqual(report.projected_eoc_total_units, 158_400)
        self.assertEqual(report.projected_eoc_cursor_units, 120_000)
        self.assertEqual(report.projected_eoc_other_units, 11_000)
        self.assertEqual(report.projected_eoc_on_demand_units, 27_400)
        self.assertEqual(report.estimated_on_demand_eoc_usd, 274.0)


if __name__ == "__main__":
    unittest.main()
