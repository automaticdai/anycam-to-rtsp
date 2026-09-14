import logging
import threading
import time

from anycam.config import ClientConfig
from anycam.receiver import LOW_LATENCY_OPTIONS, CameraReceiver


class FakeVideoFrame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format="bgr24"):
        return [[0]]


class FakeContainer:
    """Yields `count` frames, then behaves per `then`: 'end' | 'hang' | 'raise'."""

    def __init__(self, count=3, then="end"):
        self._count = count
        self._then = then
        self.closed = False
        self._released = threading.Event()

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            yield FakeVideoFrame(pts=i * 3000)
        if self._then == "raise":
            raise OSError("stream broke")
        if self._then == "hang":
            self._released.wait(timeout=5)
            raise OSError("closed while blocked")

    def close(self):
        self.closed = True
        self._released.set()


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


def test_force_reconnect_closes_a_hung_container():
    containers = []

    def opener(url, options):
        c = FakeContainer(count=1, then="hang")
        containers.append(c)
        return c

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 1)
    rx.force_reconnect()
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
    # Simulates the uninterruptible-connect window: `stop()` cannot unblock
    # this via `_close_container()`, because no container exists yet.
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
