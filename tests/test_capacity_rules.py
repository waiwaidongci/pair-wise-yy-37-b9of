import unittest

from src import capacity_rules as cr
from src.domain import ValidationError


class CapacityRulesTest(unittest.TestCase):
    def test_slot_normalize_and_expand(self):
        dt, label = cr.normalize_slot("2026-10-06T08:00:00Z", "slot_start")
        self.assertEqual(label, "2026-10-06T08:00Z")
        with self.assertRaises(ValidationError):
            cr.normalize_slot("2026-10-06T08:30:00Z", "slot_start")
        slots = cr.expand_slots(cr.parse_ts("2026-10-06T08:00Z", "f"),
                                cr.parse_ts("2026-10-06T10:00Z", "t"))
        self.assertEqual(slots, ["2026-10-06T08:00Z", "2026-10-06T09:00Z",
                                 "2026-10-06T10:00Z"])

    def test_merge_takes_max_not_sum(self):
        bases = [
            {"id": 1, "source_kind": "maintenance", "reduce_amount": 30},
            {"id": 2, "source_kind": "inspection", "reduce_amount": 50},
            {"id": 3, "source_kind": "permit", "reduce_amount": 40},
            {"id": 4, "source_kind": "audit", "reduce_amount": 20},
        ]
        merged = cr.merge_requirement(bases)
        self.assertEqual(merged["reduce_amount"], 50)  # 四份依据不叠加
        self.assertEqual(merged["basis_id"], 2)
        self.assertEqual(merged["basis_ids"], [1, 2, 3, 4])
        self.assertEqual(set(merged["sources"]), set(cr.SOURCE_KINDS))

    def test_covers_inclusive_range(self):
        basis = {"slot_start": "2026-10-06T08:00Z",
                 "slot_end": "2026-10-06T09:00Z"}
        self.assertTrue(cr.covers(basis, "2026-10-06T08:00Z"))
        self.assertTrue(cr.covers(basis, "2026-10-06T09:00Z"))
        self.assertFalse(cr.covers(basis, "2026-10-06T10:00Z"))


if __name__ == "__main__":
    unittest.main()
