import logging
import threading
import time

from anycam.config import ClientConfig
from anycam.receiver import (LOW_LATENCY_OPTIONS, OPEN_TIMEOUT_S,
                             READ_TIMEOUT_S, CameraReceiver, default_opener)


class FakeVideoFrame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format="bgr24"):
        return [[0]]


class FakeContainer:
    """Yields `count` frames, then behaves per `then`: 'end' | 'hang' | 'raise'.

    'hang' simulates a read that stops producing data and times out on its
    own, mirroring the `timeout` passed to real `av.open()` — nothing may
    close a real container from another thread any more (that was the
    segfault this fixture now protects against), so a fake must not depend
    on an external `close()` to unblock its own 'hang' branch either.

    `frame_delay_s` optionally paces yields, giving a test a window to call
    `force_reconnect()` (or otherwise act) while the container is still
    actively producing frames, rather than racing to exhaust `count` first.
    """

    def __init__(self, count=3, then="end", read_timeout_s=0.05,
                 frame_delay_s=0.0):
        self._count = count
        self._then = then
        self._read_timeout_s = read_timeout_s
        self._frame_delay_s = frame_delay_s
        self.closed = False

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            if self._frame_delay_s:
                time.sleep(self._frame_delay_s)
            yield FakeVideoFrame(pts=i * 3000)
        if self._then == "raise":
            raise OSError("stream broke")
        if self._then == "hang":
            time.sleep(self._read_timeout_s)
            raise TimeoutError("timed out waiting for data")

    def close(self):
        self.closed = True


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


FAST = ClientConfig()


def test_low_latency_options_match_spec():
    assert LOW_LATENCY_OPTIONS == {
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
        "flags": "low_delay",
        "probesize": "32",
        "analyzeduration": "0",
        "reorder_queue_size": "0",
        "max_delay": "0",
    }


def test_default_opener_passes_open_and_read_timeouts(monkeypatch):
    """The AVIOInterruptCB mechanism (PyAV's `timeout` kwarg) is what
    replaces closing a container from another thread -- confirm it is
    actually wired through, not just documented in a comment."""
    seen = {}

    def fake_av_open(url, options=None, timeout=None):
        seen["url"] = url
        seen["options"] = options
        seen["timeout"] = timeout
        return "sentinel-container"

    import av
    monkeypatch.setattr(av, "open", fake_av_open)

    result = default_opener("rtsp://h/cam0", LOW_LATENCY_OPTIONS)

    assert result == "sentinel-container"
    assert seen["url"] == "rtsp://h/cam0"
    assert seen["options"] == LOW_LATENCY_OPTIONS
    assert seen["timeout"] == (OPEN_TIMEOUT_S, READ_TIMEOUT_S)
    # Read timeout must stay under the default watchdog window so the two
    # mechanisms at least race for a no-data wedge. This is not "beats the
    # watchdog by half": measured against real PyAV, a full no-data wedge
    # actually reconnects in ~1-2s (2.01s at realtime pacing, 2.13-2.23s at
    # burst pacing) at watchdog_timeout_s=2.0, because PyAV restarts its
    # read timeout on every `av_read_frame` call rather than running one
    # continuous clock since the last frame -- so the two mechanisms race
    # rather than the read timeout reliably beating the watchdog.
    assert READ_TIMEOUT_S < ClientConfig().watchdog_timeout_s


def test_opener_receives_url_and_low_latency_options():
    seen = {}

    def opener(url, options):
        seen["url"] = url
        seen["options"] = options
        return FakeContainer(count=1)

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    wait_for(lambda: rx.frames >= 1)
    rx.stop()

    assert seen["url"] == "rtsp://h/cam0"
    assert seen["options"] == LOW_LATENCY_OPTIONS


def test_decoded_frames_land_in_the_buffer_with_identity():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=5))
    rx.start()
    assert wait_for(lambda: rx.frames >= 5)
    rx.stop()

    frame = rx.buffer.get()
    assert frame.camera_id == "cam0"
    assert frame.frame_id >= 1
    assert frame.pts is not None
    assert frame.recv_ns > 0


def test_reconnects_after_the_stream_ends():
    opened = []

    def opener(url, options):
        opened.append(url)
        return FakeContainer(count=1, then="end")

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: len(opened) >= 3)
    rx.stop()
    assert rx.reconnects >= 2


def test_reconnects_when_the_opener_raises():
    attempts = []

    def opener(url, options):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return FakeContainer(count=2)

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert len(attempts) >= 3


def test_backoff_resets_after_a_successful_frame():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=1))
    rx.start()
    assert wait_for(lambda: rx.frames >= 3)
    rx.stop()
    # Each cycle delivered a frame, so backoff never escalated past initial.
    assert rx.backoff.attempts <= 1


def test_a_stalled_read_times_out_and_reconnects_on_its_own():
    """A read that stops producing data must unblock itself via its own
    timeout — nothing may close the container from another thread to
    unblock it (that mechanism caused a real segfault against real PyAV
    and was replaced by the `timeout` passed to `av.open()`)."""
    containers = []

    def opener(url, options):
        c = FakeContainer(count=1, then="hang", read_timeout_s=0.05)
        containers.append(c)
        return c

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 1)
    assert wait_for(lambda: containers[0].closed)
    assert wait_for(lambda: len(containers) >= 2)
    rx.stop()


def test_force_reconnect_abandons_the_container_between_frames():
    """force_reconnect() sets a flag `_consume` checks once per decoded
    frame — proven here against a container that is actively producing
    frames (never blocked), which is the only case this flag can help:
    a container's own thread closes it, cleanly, once the flag is seen."""
    containers = []

    def opener(url, options):
        c = FakeContainer(count=100_000, then="end", frame_delay_s=0.002)
        containers.append(c)
        return c

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 3)
    rx.force_reconnect()
    # At 0.002s/frame, exhausting 100,000 frames would take ~200s -- far
    # past this test's timeout, so a close/reconnect this fast proves the
    # container was abandoned via the flag, not run to natural completion.
    assert wait_for(lambda: containers[0].closed)
    assert wait_for(lambda: len(containers) >= 2)
    rx.stop()


def test_watchdog_beats_on_every_frame():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=3))
    rx.start()
    assert wait_for(lambda: rx.frames >= 3)
    assert rx.watchdog.age_s() < 1.0
    rx.stop()


def test_stop_terminates_the_thread():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=100))
    rx.start()
    wait_for(lambda: rx.frames >= 1)
    rx.stop()
    assert not rx.is_alive()


from fractions import Fraction

from anycam.receiver import estimate_capture_ns


def test_estimate_uses_sender_start_time_and_pts():
    # start_time_realtime is microseconds since epoch; pts is in time_base units
    est = estimate_capture_ns(start_time_realtime=1_700_000_000_000_000,
                              pts=90000, time_base=Fraction(1, 90000))
    assert est == 1_700_000_000_000_000 * 1000 + 1_000_000_000


def test_estimate_is_none_without_a_sender_clock():
    assert estimate_capture_ns(None, 90000, Fraction(1, 90000)) is None


def test_estimate_is_none_without_pts():
    assert estimate_capture_ns(1_700_000_000_000_000, None,
                               Fraction(1, 90000)) is None


def test_estimate_is_none_without_a_time_base():
    assert estimate_capture_ns(1_700_000_000_000_000, 90000, None) is None


def test_receiver_populates_estimate_when_container_exposes_timing():
    class FakeStream:
        time_base = Fraction(1, 90000)

    class FakeStreams:
        video = [FakeStream()]

    class TimedContainer(FakeContainer):
        streams = FakeStreams()
        start_time_realtime = 1_700_000_000_000_000

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda u, o: TimedContainer(count=2))
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert rx.buffer.get().est_capture_ns is not None


def test_receiver_leaves_estimate_none_when_timing_is_unavailable():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda u, o: FakeContainer(count=2))
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert rx.buffer.get().est_capture_ns is None


def test_stop_reports_false_and_warns_when_the_thread_will_not_die(caplog):
    # Simulates a connect that ignores its own open timeout entirely (a
    # fake, unlike real `av.open()`, has no AVIOInterruptCB of its own):
    # `stop()` no longer tries to unblock this by closing anything — a
    # container is only ever closed by the thread that owns it, and here
    # there isn't one yet — so it can only wait and report False.
    release = threading.Event()

    def opener(url, options):
        release.wait(timeout=5)
        raise OSError("connect timed out")

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    wait_for(lambda: rx.is_alive())
    try:
        with caplog.at_level(logging.WARNING, logger="anycam.receiver"):
            stopped = rx.stop(timeout=0.1)
        assert stopped is False
        assert rx.is_alive()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("cam0" in r.getMessage() for r in warnings)
    finally:
        # Let the blocked opener return so the background thread can exit
        # cleanly and not leak into other tests.
        release.set()
        rx.join(timeout=2.0)
