"""Aggregated-usage responses omit `aggregations` when the window has none."""

from __future__ import annotations

import unittest

from cursor_usage.cursor_client import CursorSchemaError, _aggregation_rows


class AggregationRowsTests(unittest.TestCase):
    def test_rows_come_from_aggregations(self) -> None:
        rows = _aggregation_rows(
            {
                "aggregations": [{"modelIntent": "grok-4.7-high", "tier": 1, "totalCents": 10}],
                "totalCostCents": 10,
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["modelIntent"], "grok-4.7-high")

    def test_empty_object_is_no_included_usage(self) -> None:
        self.assertEqual(_aggregation_rows({}), [])

    def test_unknown_shape_still_raises(self) -> None:
        with self.assertRaises(CursorSchemaError):
            _aggregation_rows({"usage": []})


if __name__ == "__main__":
    unittest.main()
