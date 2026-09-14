import threading
import time

from anycam.config import (AppConfig, CameraConfig, ClientConfig, EncodeConfig,
                           ServerConfig, SourceConfig, VideoConfig)
from anycam.client import MultiCameraClient


def app_config(n=3):
    cams = tuple(
        CameraConfig(id=f"cam{i}",
                     source=SourceConfig(type="dshow", device=f"Camera {i}"),
                     video=VideoConfig(), encode=EncodeConfig())
        for i in range(n))
    return AppConfig(server=ServerConfig(rtsp_port=8554),
                     cameras=cams, client=ClientConfig())


class FakeVideoFrame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format="bgr24"):
        return [[0]]


class FakeContainer:
    def __init__(self, count=1000, stall_after=None):
        self._count = count
        self._stall_after = stall_after
        self.closed = False
        self._released = threading.Event()

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            if self._stall_after is not None and i >= self._stall_after:
                self._released.wait(timeout=10)
                raise OSError("closed while stalled")
            yield FakeVideoFrame(pts=i * 3000)
            time.sleep(0.002)

    def close(self):
        self.closed = True
        self._released.set()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_starts_one_receiver_per_camera():
    with MultiCameraClient(app_config(3), host="172.30.64.1",
                           opener=lambda u, o: FakeContainer()) as client:
        assert client.camera_ids == ("cam0", "cam1", "cam2")
        assert wait_for(lambda: all(
            client.latest(c) is not None for c in client.camera_ids))


def test_builds_rtsp_urls_from_the_discovered_host():
    seen = []

    def opener(url, options):
        seen.append(url)
        return FakeContainer()

    with MultiCameraClient(app_config(2), host="172.30.64.1",
                           opener=opener):
        wait_for(lambda: len(seen) >= 2)

    assert "rtsp://172.30.64.1:8554/cam0" in seen
    assert "rtsp://172.30.64.1:8554/cam1" in seen


def test_latest_returns_the_newest_frame_for_that_camera():
    with MultiCameraClient(app_config(2), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        first = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > first)


def test_take_consumes_so_the_next_frame_is_not_a_drop():
    with MultiCameraClient(app_config(1), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        assert wait_for(lambda: client.take("cam0") is not None)
        assert client.latest("cam0") is None


def test_one_dead_camera_does_not_affect_the_others():
    """The property the whole architecture exists to provide."""
    def opener(url, options):
        if url.endswith("cam1"):
            raise OSError("camera 1 is unplugged")
        return FakeContainer()

    with MultiCameraClient(app_config(3), host="h", opener=opener) as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        assert wait_for(lambda: client.latest("cam2") is not None)
        assert client.latest("cam1") is None
        assert client.stats()["cam1"].reconnects >= 1

        healthy = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > healthy)


def test_monitor_forces_reconnect_on_a_wedged_stream():
    """A wedged camera holds a healthy connection; only the watchdog sees it."""
    cfg = app_config(1)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.2))
    containers = []

    def opener(url, options):
        c = FakeContainer(stall_after=2)
        containers.append(c)
        return c

    with MultiCameraClient(cfg, host="h", opener=opener,
                           monitor_interval_s=0.02) as client:
        assert wait_for(lambda: len(containers) >= 2, timeout=5.0)
        assert containers[0].closed
        assert client.stats()["cam0"].reconnects >= 1


def test_stats_reports_every_camera():
    with MultiCameraClient(app_config(3), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        wait_for(lambda: client.latest("cam2") is not None)
        stats = client.stats()
        assert sorted(stats) == ["cam0", "cam1", "cam2"]
        assert stats["cam0"].frames > 0


def test_stop_is_idempotent():
    client = MultiCameraClient(app_config(2), host="h",
                               opener=lambda u, o: FakeContainer())
    client.start()
    client.stop()
    client.stop()
