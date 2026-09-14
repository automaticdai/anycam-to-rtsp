import time

import pytest

from anycam.client import MultiCameraClient

pytestmark = pytest.mark.integration


def wait_for(predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_frames_arrive_from_both_synthetic_cameras(synthetic_stack):
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        assert wait_for(lambda: client.latest("cam1") is not None)

        frame = client.latest("cam0")
        assert frame.image.shape == (480, 640, 3)
        assert frame.camera_id == "cam0"


def test_frames_advance_over_time(synthetic_stack):
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        first = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > first + 30)


def test_buffer_depth_never_exceeds_one_under_a_slow_consumer(synthetic_stack):
    """A consumer slower than the stream must lose frames, not gain latency."""
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        for _ in range(10):
            time.sleep(0.3)          # far slower than 30fps
            assert client.stats()["cam0"].depth <= 1
        stats = client.stats()["cam0"]
        assert stats.dropped > 0, "a slow consumer must be dropping frames"


def test_client_reconnects_after_the_server_restarts(synthetic_stack):
    config, proc = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        before = client.stats()["cam0"].reconnects

        proc.terminate()
        proc.wait(timeout=10)
        assert wait_for(
            lambda: client.stats()["cam0"].reconnects > before, timeout=20)
