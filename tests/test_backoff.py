from anycam.backoff import Backoff
from anycam.config import BackoffConfig

CFG = BackoffConfig(initial_s=0.2, factor=2.0, max_s=5.0)


def test_first_delay_is_initial():
    assert Backoff(CFG).next_delay() == 0.2


def test_delays_double_up_to_the_cap():
    b = Backoff(CFG)
    assert [b.next_delay() for _ in range(8)] == [
        0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0]


def test_reset_returns_to_initial():
    b = Backoff(CFG)
    for _ in range(5):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 0.2


def test_attempts_counts_delays_since_reset():
    b = Backoff(CFG)
    b.next_delay()
    b.next_delay()
    assert b.attempts == 2
    b.reset()
    assert b.attempts == 0
