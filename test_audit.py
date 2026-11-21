"""Unit tests for the OFFLINE diagnostic tool (not a submission test suite)."""
import itertools
import json
import tempfile
import unittest
from pathlib import Path

import audit_blast as audit


class AuditTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(audit.pack(set(), {}), [])
        self.assertEqual(audit.lower_bound(set(), {}), 0)

    def test_all_tests_kept_exactly_once(self):
        d = {0: 28817, 1: 21454, 2: 6686, 3: 3863, 4: 3537, 5: 2533}
        workers = audit.pack(set(d), d)
        self.assertEqual(sorted(n for w in workers for n in w), sorted(d))
        self.assertLessEqual(len(workers), 8)
        self.assertLessEqual(audit.lower_bound(set(d), d), audit.worker_cost(workers, d))

    def test_bound_against_exhaustive_small_examples(self):
        for times in ((1, 2, 3), (5000, 3000, 2000), (9000, 8000, 7000, 1000), (0, 0, 0)):
            d = dict(enumerate(times))
            optimum = None
            for k in range(1, len(times) + 1):
                for assignment in itertools.product(range(k), repeat=len(times)):
                    bins = [[] for _ in range(k)]
                    for n, w in enumerate(assignment):
                        bins[w].append(n)
                    cost = audit.worker_cost(bins, d)
                    optimum = cost if optimum is None else min(optimum, cost)
            self.assertLessEqual(audit.lower_bound(set(d), d), optimum)
            self.assertGreaterEqual(audit.worker_cost(audit.pack(set(d), d), d), optimum)

    def test_validation(self):
        d = {1: 100, 2: 200}
        self.assertEqual(audit.normalize_workers([[1.0], [], [2]], d), [[1], [2]])
        for value in ([[True]], [[1, 1]], [[3]], [[1.25]], [[float('nan')]]):
            with self.assertRaises(ValueError):
                audit.normalize_workers(value, d)

    def test_miss_penalty_requires_all_failures(self):
        tests = [{'number': 1, 'file': 'tests/test_a.py', 'id': 'a', 'seconds': .1},
                 {'number': 2, 'file': 'tests/test_a.py', 'id': 'b', 'seconds': .2}]
        result = audit.evaluate({'c': [[1]]}, {'c': {'failing': [1, 2]}}, tests, {1: 100, 2: 200})
        self.assertEqual(result['missed_commits'], 1)
        self.assertEqual(result['simulated_seconds'], 302.1)


if __name__ == '__main__':
    unittest.main()
