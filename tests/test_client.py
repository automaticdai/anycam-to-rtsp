import logging
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
    """`stall_after` simulates a wedged camera: a healthy connection that
    simply stops producing frames. It times out and raises on its own
    (mirroring the `timeout` passed to real `av.open()`) rather than
    waiting to be closed externally -- nothing may close a real container
    from another thread any more (that was the segfault this fixture now
    protects against), so a fake must not depend on that either."""

    def __init__(self, count=1000, stall_after=None, stall_timeout_s=0.3):
        self._count = count
        self._stall_after = stall_after
        self._stall_timeout_s = stall_timeout_s
        self.closed = False

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            if self._stall_after is not None and i >= self._stall_after:
                time.sleep(self._stall_timeout_s)
                raise TimeoutError("timed out waiting for data")
            yield FakeVideoFrame(pts=i * 3000)
            time.sleep(0.002)

    def close(self):
        self.closed = True


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


def test_monitor_reconnects_only_the_wedged_camera_among_healthy_peers():
    """Isolation claim: force-reconnecting one wedged camera must not touch
    its healthy peers, whose reconnect counts must stay unchanged throughout
    and which must keep delivering fresh frames the whole time."""
    cfg = app_config(3)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.2))

    def opener(url, options):
        if url.endswith("cam1"):
            return FakeContainer(stall_after=2)
        return FakeContainer()

    with MultiCameraClient(cfg, host="h", opener=opener,
                           monitor_interval_s=0.02) as client:
        assert wait_for(lambda: client.latest("cam0") is not None
                         and client.latest("cam2") is not None)

        # Poll continuously while cam1 is expected to wedge and get
        # force-reconnected; assert the healthy peers' reconnect counts
        # never move during that whole window, not just at the end.
        deadline = time.monotonic() + 3.0
        wedge_reconnected = False
        while time.monotonic() < deadline and not wedge_reconnected:
            stats = client.stats()
            assert stats["cam0"].reconnects == 0
            assert stats["cam2"].reconnects == 0
            wedge_reconnected = stats["cam1"].reconnects >= 1
            time.sleep(0.01)
        assert wedge_reconnected

        # The healthy peers must still be actively delivering frames, not
        # just untouched in their counters.
        healthy0 = client.latest("cam0").frame_id
        healthy2 = client.latest("cam2").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > healthy0)
        assert wait_for(lambda: client.latest("cam2").frame_id > healthy2)


def test_monitor_survives_a_per_camera_exception(caplog):
    """One camera's monitor-loop failure must not stop supervision of the
    rest: the monitor thread must keep running and keep servicing every
    other camera's watchdog."""
    cfg = app_config(2)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.2))

    def opener(url, options):
        return FakeContainer(stall_after=2)

    with MultiCameraClient(cfg, host="h", opener=opener,
                           monitor_interval_s=0.02) as client:
        assert wait_for(lambda: client.latest("cam0") is not None
                         and client.latest("cam1") is not None)

        # Inject a failure into cam0's monitor check only; keep everything
        # else about the client and its receivers real.
        broken = client._receivers["cam0"]

        def boom():
            raise RuntimeError("simulated monitor-loop failure")

        broken.watchdog.expired = boom

        with caplog.at_level(logging.WARNING, logger="anycam.client"):
            # cam1 must still get force-reconnected despite cam0 raising on
            # every single monitor tick.
            assert wait_for(lambda: client.stats()["cam1"].reconnects >= 1,
                            timeout=5.0)
            assert client._monitor.is_alive()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cam0" in r.getMessage() for r in warnings)
    assert any(r.exc_info for r in warnings)
