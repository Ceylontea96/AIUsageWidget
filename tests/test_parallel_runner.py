"""The parallel test runner splits work sensibly and reports honestly."""
import unittest

from tests import parallel
from tests.support import integration


class BatchTests(unittest.TestCase):
    ids = [f'tests.test_a.Big.test_{n}' for n in range(10)] + ['tests.test_b.Small.test_0', 'tests.test_b.Small.test_1']

    def test_without_times_classes_split_into_small_batches(self):
        plan = parallel.batches(self.ids, {})
        self.assertEqual(sorted(len(batch) for batch in plan), [2, 2, parallel.BATCH_TESTS])
        for batch in plan:
            self.assertEqual(len({test_id.rsplit('.', 1)[0] for test_id in batch}), 1, 'one class per batch')
        self.assertEqual(sorted(sum(plan, [])), sorted(self.ids), 'every test once')

    def test_with_times_the_longest_batch_goes_first(self):
        times = {test_id: 0.1 for test_id in self.ids}
        times['tests.test_b.Small.test_0'] = 30.0
        plan = parallel.batches(self.ids, times)
        self.assertIn('tests.test_b.Small.test_0', plan[0])
        self.assertEqual(sorted(sum(plan, [])), sorted(self.ids))

    def test_integration_classes_are_marked_for_their_own_phase(self):
        @integration('starts PowerShell')
        class Slow(unittest.TestCase):
            def test_x(self):
                pass

        class Quick(unittest.TestCase):
            def test_x(self):
                pass

        self.assertTrue(getattr(Slow, 'integration', False))
        self.assertFalse(getattr(Quick, 'integration', False))


class WorkerReportTests(unittest.TestCase):
    def test_a_batch_reports_results_and_times(self):
        report = parallel.run_ids(['tests.test_parallel_runner.BatchTests.test_integration_classes_are_marked_for_their_own_phase'])
        self.assertEqual((report['ran'], report['failures'], report['errors']), (1, [], []))
        self.assertEqual(len(report['times']), 1)

    def test_a_missing_test_is_an_error_not_a_silent_pass(self):
        report = parallel.run_ids(['tests.test_parallel_runner.NoSuchTests.test_nothing'])
        self.assertTrue(report['errors'])


if __name__ == '__main__':
    unittest.main()
