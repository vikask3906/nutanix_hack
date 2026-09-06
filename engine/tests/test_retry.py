import unittest

from engine.retry import base_delay_ms, delay_seconds, max_attempts, should_retry


class FakeRng:
    """Deterministic stand-in for random, so jitter is testable."""

    def __init__(self, fraction):
        self.fraction = fraction

    def uniform(self, low, high):
        return low + (high - low) * self.fraction


class MaxAttemptsTests(unittest.TestCase):
    def test_no_retry_block_means_one_attempt(self):
        self.assertEqual(max_attempts({"id": "a"}), 1)

    def test_reads_the_retry_block(self):
        self.assertEqual(max_attempts({"id": "a", "retry": {"max": 5}}), 5)

    def test_never_below_one(self):
        self.assertEqual(max_attempts({"id": "a", "retry": {"max": 0}}), 1)


class ShouldRetryTests(unittest.TestCase):
    def test_retries_while_attempts_remain(self):
        step = {"id": "a", "retry": {"max": 3}}
        self.assertTrue(should_retry(step, 1))
        self.assertTrue(should_retry(step, 2))
        self.assertFalse(should_retry(step, 3))

    def test_no_retry_block_never_retries(self):
        self.assertFalse(should_retry({"id": "a"}, 1))


class BackoffTests(unittest.TestCase):
    def test_exponential_doubles(self):
        retry = {"backoff": "exponential", "base_ms": 500}
        self.assertEqual(base_delay_ms(1, retry), 500)
        self.assertEqual(base_delay_ms(2, retry), 1000)
        self.assertEqual(base_delay_ms(3, retry), 2000)
        self.assertEqual(base_delay_ms(4, retry), 4000)

    def test_linear_grows_by_base(self):
        retry = {"backoff": "linear", "base_ms": 300}
        self.assertEqual(base_delay_ms(1, retry), 300)
        self.assertEqual(base_delay_ms(3, retry), 900)

    def test_fixed_does_not_grow(self):
        retry = {"backoff": "fixed", "base_ms": 750}
        self.assertEqual(base_delay_ms(1, retry), 750)
        self.assertEqual(base_delay_ms(9, retry), 750)

    def test_capped_at_max_ms(self):
        retry = {"backoff": "exponential", "base_ms": 1000, "max_ms": 5000}
        self.assertEqual(base_delay_ms(10, retry), 5000)

    def test_huge_attempt_numbers_do_not_explode(self):
        # Without an exponent guard, 2**200 is computed before the cap applies.
        retry = {"backoff": "exponential", "base_ms": 1000, "max_ms": 60000}
        self.assertEqual(base_delay_ms(200, retry), 60000)

    def test_defaults_when_no_retry_block(self):
        self.assertEqual(base_delay_ms(1, None), 500)


class JitterTests(unittest.TestCase):
    """Full jitter: uniform over [0, base]. The point is that identical failures
    do not produce identical retry times, so a recovering service is not hit by
    a synchronised stampede."""

    def test_jitter_spans_zero_to_base(self):
        retry = {"backoff": "exponential", "base_ms": 1000}
        self.assertAlmostEqual(delay_seconds(1, retry, FakeRng(0.0)), 0.0)
        self.assertAlmostEqual(delay_seconds(1, retry, FakeRng(1.0)), 1.0)
        self.assertAlmostEqual(delay_seconds(1, retry, FakeRng(0.5)), 0.5)

    def test_jitter_scales_with_the_backoff(self):
        retry = {"backoff": "exponential", "base_ms": 1000}
        self.assertAlmostEqual(delay_seconds(3, retry, FakeRng(1.0)), 4.0)

    def test_two_callers_get_different_delays(self):
        retry = {"backoff": "fixed", "base_ms": 1000}
        a = delay_seconds(1, retry, FakeRng(0.2))
        b = delay_seconds(1, retry, FakeRng(0.8))
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
