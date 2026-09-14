from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .backoff import Backoff
from .config import ClientConfig
from .frame import Frame
from .freshness import LatestFrameBuffer
from .watchdog import Watchdog

log = logging.getLogger(__name__)

# Mandatory. The RTSP demuxer's jitter buffer can silently add more delay than
# the entire rest of the pipeline, and it is invisible without measurement.
LOW_LATENCY_OPTIONS: dict[str, str] = {
    "rtsp_transport": "tcp",
    "fflags": "nobuffer",
    "flags": "low_delay",
    "probesize": "32",
    "analyzeduration": "0",
    "reorder_queue_size": "0",
    "max_delay": "0",
}


def default_opener(url: str, options: dict[str, str]) -> Any:
    import av
    return av.open(url, options=options)


class CameraReceiver(threading.Thread):
    """Receives one camera's RTSP stream, decoding into a newest-frame buffer.

    Runs forever: a closed stream, a refused connection or a forced reconnect
    are all normal states that lead back to `connect()`, never to exit.
    """

    def __init__(self, camera_id: str, url: str, client_config: ClientConfig, *,
                 opener: Callable[[str, dict[str, str]], Any] = default_opener,
                 buffer: LatestFrameBuffer | None = None,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        super().__init__(name=f"receiver-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.url = url
        self.buffer = buffer if buffer is not None else LatestFrameBuffer()
        self.watchdog = Watchdog(client_config.watchdog_timeout_s, clock=clock)
        self.backoff = Backoff(client_config.backoff)
        self.frames = 0
        self.reconnects = 0
        self.last_error: str | None = None

        self._opener = opener
        self._clock = clock
        self._stop_event = threading.Event()
        self._container_lock = threading.Lock()
        self._container: Any | None = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._close_container()
        self.join(timeout=timeout)

    def force_reconnect(self) -> None:
        """Close the container so a blocked read raises and the loop retries.

        A wedged stream cannot be interrupted from inside the decode loop,
        because the read never returns. Closing from outside is what unblocks
        it, which is why the watchdog lives outside this thread.
        """
        log.warning("%s: forcing reconnect", self.camera_id)
        self._close_container()

    def _close_container(self) -> None:
        with self._container_lock:
            container, self._container = self._container, None
        if container is not None:
            try:
                container.close()
            except Exception:
                log.debug("%s: error closing container", self.camera_id,
                          exc_info=True)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                container = self._opener(self.url, LOW_LATENCY_OPTIONS)
            except Exception as exc:
                self.last_error = str(exc)
                self.reconnects += 1
                self._wait(self.backoff.next_delay())
                continue

            with self._container_lock:
                self._container = container
            try:
                self._consume(container)
            except Exception as exc:
                self.last_error = str(exc)
                log.info("%s: stream ended: %s", self.camera_id, exc)
            finally:
                self._close_container()

            if not self._stop_event.is_set():
                self.reconnects += 1
                self._wait(self.backoff.next_delay())

    def _consume(self, container: Any) -> None:
        start_realtime, time_base = _sender_clock(container)
        for decoded in container.decode(video=0):
            if self._stop_event.is_set():
                return
            recv_ns = self._clock()
            image = decoded.to_ndarray(format="bgr24")
            self.frames += 1
            self.buffer.put(Frame(
                camera_id=self.camera_id,
                frame_id=self.frames,
                pts=decoded.pts,
                recv_ns=recv_ns,
                decoded_ns=self._clock(),
                image=image,
                est_capture_ns=estimate_capture_ns(
                    start_realtime, decoded.pts, time_base),
            ))
            self.watchdog.beat()
            self.backoff.reset()

    def _wait(self, seconds: float) -> None:
        self._stop_event.wait(timeout=seconds)


def estimate_capture_ns(start_time_realtime: int | None, pts: int | None,
                        time_base: Any | None) -> int | None:
    """Estimate capture wall-clock from the sender's clock, or None.

    `start_time_realtime` is microseconds since the epoch, as reported by the
    container via RTCP sender reports. This is explicitly an estimate: the
    spec treats the authoritative latency measurement as the out-of-band
    calibration harness, because no in-band timestamp can account for sensor
    exposure and USB transfer.
    """
    if start_time_realtime is None or pts is None or time_base is None:
        return None
    return int(start_time_realtime) * 1000 + int(pts * float(time_base) * 1e9)


def _sender_clock(container: Any) -> tuple[int | None, Any | None]:
    """Read (start_time_realtime, time_base), tolerating containers without them."""
    streams = getattr(container, "streams", None)
    video = getattr(streams, "video", None) if streams is not None else None
    if not video:
        return None, None
    return (getattr(container, "start_time_realtime", None),
            getattr(video[0], "time_base", None))
