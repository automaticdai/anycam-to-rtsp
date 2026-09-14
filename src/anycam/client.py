from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .config import AppConfig
from .frame import Frame
from .receiver import CameraReceiver, default_opener
from .stats import StreamStats

log = logging.getLogger(__name__)


class MultiCameraClient:
    """Owns one independent receiver per camera plus a shared monitor thread.

    Cameras share no state. A camera that dies, wedges or never appears
    affects only its own receiver, buffer, watchdog and backoff.
    """

    def __init__(self, config: AppConfig, host: str, *,
                 opener: Callable[[str, dict[str, str]], Any] = default_opener,
                 monitor_interval_s: float = 0.25) -> None:
        self._config = config
        self._host = host
        self._monitor_interval_s = monitor_interval_s
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._receivers: dict[str, CameraReceiver] = {
            cam.id: CameraReceiver(
                cam.id, self._url(cam.id), config.client, opener=opener)
            for cam in config.cameras
        }

    def _url(self, camera_id: str) -> str:
        return f"rtsp://{self._host}:{self._config.server.rtsp_port}/{camera_id}"

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._receivers)

    def start(self) -> None:
        for rx in self._receivers.values():
            rx.start()
        self._monitor = threading.Thread(
            target=self._run_monitor, name="watchdog-monitor", daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5.0)
            self._monitor = None
        for rx in self._receivers.values():
            if rx.is_alive():
                rx.stop()

    def latest(self, camera_id: str) -> Frame | None:
        return self._receivers[camera_id].buffer.get()

    def take(self, camera_id: str) -> Frame | None:
        return self._receivers[camera_id].buffer.take()

    def stats(self) -> dict[str, StreamStats]:
        return {
            cid: StreamStats(
                camera_id=cid,
                frames=rx.frames,
                dropped=rx.buffer.dropped,
                reconnects=rx.reconnects,
                age_s=rx.watchdog.age_s(),
                depth=rx.buffer.depth,
                last_error=rx.last_error,
            )
            for cid, rx in self._receivers.items()
        }

    def _run_monitor(self) -> None:
        """Force a reconnect on any stream whose watchdog has expired.

        This cannot live inside the receiver: a wedged stream is blocked
        inside a read that never returns, so only another thread can close
        the container and unblock it.
        """
        while not self._stop.wait(self._monitor_interval_s):
            for cid, rx in self._receivers.items():
                if rx.watchdog.expired():
                    log.warning("%s: no frames for %.2fs, reconnecting",
                                cid, rx.watchdog.age_s())
                    rx.watchdog.beat()
                    rx.force_reconnect()

    def __enter__(self) -> MultiCameraClient:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
