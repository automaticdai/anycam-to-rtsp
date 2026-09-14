from anycam.watchdog import Watchdog


class FakeClock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1e9)


def test_fresh_watchdog_is_not_expired():
    assert not Watchdog(2.0, clock=FakeClock()).expired()


def test_expires_after_timeout_without_a_beat():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(1.9)
    assert not wd.expired()
    clock.advance(0.2)
    assert wd.expired()


def test_beat_resets_the_timer():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(1.9)
    wd.beat()
    clock.advance(1.9)
    assert not wd.expired()


def test_age_reports_time_since_last_beat():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(0.75)
    assert abs(wd.age_s() - 0.75) < 1e-6


def test_a_wedged_stream_expires_even_though_nothing_raised():
    """The failure mode this exists for: the socket is fine, frames stopped."""
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    for _ in range(30):          # one second of healthy 30fps
        clock.advance(1 / 30)
        wd.beat()
    assert not wd.expired()
    clock.advance(2.5)           # camera wedges, connection stays open
    assert wd.expired()
