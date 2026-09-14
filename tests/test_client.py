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
    protects against), so a fake must not depend on that either.

    `frame_interval_s` paces yielded frames. The default (0.002s) is fast
    enough not to slow most tests down; a value close to or above
    `watchdog_timeout_s` instead simulates a camera that is alive and
    delivering, just too slowly to beat the watchdog between frames -- the
    one stall shape the read timeout cannot resolve on its own, because
    data never actually stops arriving.
    """

    def __init__(self, count=1000, stall_after=None, stall_timeout_s=0.3,
                 frame_interval_s=0.002):
        self._count = count
        self._stall_after = stall_after
        self._stall_timeout_s = stall_timeout_s
        self._frame_interval_s = frame_interval_s
        self.closed = False

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            if self._stall_after is not None and i >= self._stall_after:
                time.sleep(self._stall_timeout_s)
                raise TimeoutError("timed out waiting for data")
            yield FakeVideoFrame(pts=i * 3000)
            time.sleep(self._frame_interval_s)

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


def test_a_stalled_stream_self_recovers_via_the_read_timeout():
    """A camera whose connection stops producing data entirely does not
    need the monitor: CameraReceiver's own read timeout aborts the blocked
    read and the receiver reconnects by itself, with the monitor's
    watchdog check and force_reconnect() call never actually mattering to
    the outcome (this test still passes with the monitor thread replaced
    by a no-op). See test_monitor_reconnects_a_stream_that_is_too_slow_
    for_the_watchdog below for the one stall shape that genuinely requires
    the monitor."""
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


def test_monitor_reconnects_a_stream_that_is_too_slow_for_the_watchdog():
    """The one thing only the monitor can do: a stream that keeps
    delivering frames, just slower than watchdog_timeout_s, never trips
    the read timeout (data keeps arriving, just not fast enough) and never
    raises on its own. `_consume` only beats the watchdog when a frame
    actually arrives, so only watchdog expiry -> force_reconnect() -> the
    flag `_consume` checks between frames can move this stream at all. With
    `_run_monitor` replaced by a no-op loop, this reconnect never happens."""
    cfg = app_config(1)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.2))
    containers = []

    def opener(url, options):
        c = FakeContainer(frame_interval_s=0.5)
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


def test_a_stalled_camera_recovering_does_not_disturb_its_healthy_peers():
    """Isolation claim: cam1 stalling and recovering on its own (via the
    read timeout -- see test_a_stalled_stream_self_recovers_via_the_read_
    timeout above for that mechanism by itself) must not touch its healthy
    peers, whose reconnect counts must stay unchanged throughout and which
    must keep delivering fresh frames the whole time. The monitor thread is
    still running here and still polls every camera's watchdog each tick;
    what this test proves is that doing so for a stalled cam1 has no
    observable effect on cam0/cam2."""
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

        # Poll continuously while cam1 is expected to stall and recover;
        # assert the healthy peers' reconnect counts never move during
        # that whole window, not just at the end.
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


def test_a_never_connecting_camera_reports_stalled_consistently():
    """Before this fix, the monitor rate-limited its own force_reconnect()
    calls by calling rx.watchdog.beat() -- the very clock StreamStats.age_s
    reads -- so age_s sawtoothed 0 -> timeout -> 0 forever and a camera that
    never delivered a single frame reported STALLED on only a small
    fraction of samples (measured: 4 of 60). A camera that never delivers a
    frame must report STALLED on every sample once the timeout has first
    elapsed, not intermittently."""
    cfg = app_config(1)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.1))

    with MultiCameraClient(cfg, host="h",
                           opener=lambda u, o: FakeContainer(count=0),
                           monitor_interval_s=0.02) as client:
        timeout_s = cfg.client.watchdog_timeout_s
        # Let the watchdog timeout elapse at least once before sampling.
        time.sleep(timeout_s * 3)

        samples = []
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            samples.append(client.stats()["cam0"].is_stalled(timeout_s))
            time.sleep(0.02)

    assert len(samples) >= 10
    assert all(samples), (
        "a camera that never delivered a frame must be STALLED on every "
        "sample, not intermittently")


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
