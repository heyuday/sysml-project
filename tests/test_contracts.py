"""Small future unit checks. No database, Redis, or model calls."""

import unittest
from sysml.identity import analyze
from sysml.policies import Config, Cache, affected

CAT = {"stock": {"s_i_id": "int4", "s_quantity": "int4", "s_ytd": "numeric"}}
SQL = "SELECT s_quantity FROM stock WHERE s_i_id=$1"


def analysis(sql=SQL, params=None, scope="a", normal="N2"):
    return analyze(
        sql, [42] if params is None else params, {"scope": scope}, normal, CAT
    )


class Contracts(unittest.TestCase):
    def test_key_isolation_and_fallback(self):
        a = analysis()
        self.assertIsNotNone(a.key)
        self.assertEqual(a.key, analysis(SQL.replace("SELECT", "select")).key)
        self.assertNotEqual(a.key, analysis(params=[43]).key)
        self.assertNotEqual(a.key, analysis(scope="b").key)
        self.assertIsNone(analysis("SELECT random() FROM stock").key)
        self.assertIsNone(analysis(SQL + " FOR UPDATE").key)

    def test_old_and_new_images(self):
        a = analysis()
        change = {
            "table": "stock",
            "op": "UPDATE",
            "columns": ["s_i_id"],
            "old": {"s_i_id": 42},
            "new": {"s_i_id": 43},
        }
        self.assertTrue(affected(a, change, "I3"))
        self.assertTrue(
            affected(a, {**change, "old": {"s_i_id": 41}, "new": {"s_i_id": 42}}, "I3")
        )
        self.assertFalse(affected(a, {**change, "old": {"s_i_id": 41}}, "I3"))
        self.assertFalse(affected(a, {**change, "columns": ["s_ytd"]}, "I2"))

    def test_revision_retains_other_points_and_history(self):
        cache = Cache(Config(invalid="I4"))
        a, b = analysis(), analysis(params=[43])
        for item in [a, b]:
            cache.access(item, {}, 0)
            cache.publish(item.key, 8, 3, 0)
        cache.changes(
            [
                {
                    "table": "stock",
                    "columns": ["s_quantity"],
                    "op": "UPDATE",
                    "old": {"s_i_id": 42},
                    "new": {"s_i_id": 42},
                }
            ],
            1,
        )
        self.assertFalse(cache.valid(a.key))
        self.assertTrue(cache.valid(b.key))
        self.assertEqual(cache.hist[a.key].freq, 1)
        self.assertEqual(cache.used, 8)

    def test_capacity_and_reconsider(self):
        c = Cache(Config(capacity=8, admission="reconsider", min_admit=100))
        a, b = analysis(), analysis(params=[43])
        for item in [a, b]:
            c.access(item, {}, 0)
            self.assertTrue(c.publish(item.key, 8, 1, 0))
        self.assertEqual(c.used, 8)
        self.assertIn(a.key, c.hist)
        c.changes([{"table": "stock", "op": "TRUNCATE", "columns": []}], 1)
        self.assertFalse(c.publish(b.key, 8, 1, 2))
