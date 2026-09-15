import os
import signal
import time

import pytest

from anycam2rtsp.client import MultiCameraClient

from .conftest import _start_mediamtx, _terminate

pytestmark = pytest.mark.integration


def wait_for(predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_frames_arrive_from_both_synthetic_cameras(synthetic_stack):
    config, _, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        assert wait_for(lambda: client.latest("cam1") is not None)

        frame = client.latest("cam0")
        assert frame.image.shape == (480, 640, 3)
        assert frame.camera_id == "cam0"


def test_frames_advance_over_time(synthetic_stack):
    config, _, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        first = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > first + 30)


def test_buffer_depth_never_exceeds_one_under_a_slow_consumer(synthetic_stack):
    """A consumer slower than the stream must lose frames, not gain latency."""
    config, _, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        for _ in range(10):
            time.sleep(0.3)          # far slower than 30fps
            assert client.stats()["cam0"].depth <= 1
        stats = client.stats()["cam0"]
        assert stats.dropped > 0, "a slow consumer must be dropping frames"


def test_client_reconnects_after_the_server_restarts(synthetic_stack):
    """The spec's layer-2 promise: when MediaMTX itself comes back after
    dying, every client reconnects and frames resume -- not merely that the
    reconnect counter ticks up while the server stays dead.

    `LatestFrameBuffer` is never cleared on reconnect, and `frame_id` never
    resets either (it just keeps counting up for the receiver's whole
    lifetime), so a baseline sampled before the kill can be beaten forever
    by the one stale frame left sitting in the buffer -- even with the
    server permanently dead. To make "frames resumed" mean something, the
    buffer is drained with `take()` only *after* the reconnect counter has
    already ticked up: by that point the old receiver's `_consume` loop has
    necessarily already exited (that is what incremented `reconnects`), so
    no frame from the dead connection can land in the buffer after this
    `take()` -- only a frame decoded from a fresh connection can satisfy
    the assertion below.
    """
    config, proc, cfg_path = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        before_reconnects = client.stats()["cam0"].reconnects

        proc.terminate()
        proc.wait(timeout=10)

        new_proc = _start_mediamtx(cfg_path, config.server.rtsp_port)
        try:
            assert wait_for(
                lambda: client.stats()["cam0"].reconnects > before_reconnects,
                timeout=20)

            client.take("cam0")
            assert wait_for(lambda: client.latest("cam0") is not None,
                            timeout=20)
        finally:
            _terminate(new_proc)


def test_client_recovers_from_a_read_timeout_when_the_server_wedges(
        synthetic_stack):
    """The read timeout, not the watchdog, must unblock a genuinely wedged
    read. SIGSTOP-ing the whole mediamtx+ffmpeg process group (mediamtx was
    started with start_new_session=True, so its pid is the process group
    id, and its runOnInit ffmpeg children inherit that group) freezes RTP
    delivery without closing the TCP connection -- the same shape as a
    wedged camera. `av.open()`'s own read timeout must notice on its own
    and force a reconnect; SIGCONT must let frames flow again once the
    group resumes. This is asserted against a real PyAV/MediaMTX pair
    precisely because the segfault this suite guards against was invisible
    to the fakes-only unit tests."""
    config, proc, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        before = client.stats()["cam0"].reconnects

        os.killpg(proc.pid, signal.SIGSTOP)
        try:
            assert wait_for(
                lambda: client.stats()["cam0"].reconnects > before,
                timeout=3.0)
        finally:
            os.killpg(proc.pid, signal.SIGCONT)

        healthy = client.latest("cam0").frame_id
        assert wait_for(
            lambda: client.latest("cam0") is not None
            and client.latest("cam0").frame_id > healthy, timeout=10.0)
