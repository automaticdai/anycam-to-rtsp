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

# Passed to PyAV's `timeout=(open, read)` kwarg, which wires FFmpeg's
# AVIOInterruptCB -- the mechanism FFmpeg provides for aborting a blocked I/O
# call from the SAME thread that issued it. This replaces closing the
# container from another thread to unblock a wedged read: that is a
# use-after-free against a real AVFormatContext (confirmed by a real
# segfault under Task 10's integration suite), not just a documented risk,
# so no thread other than the one running `decode()` may touch a container
# once it is open.
#
# READ_TIMEOUT_S bounds how long a blocked read can go without data before
# raising on its own. It is kept well under ClientConfig's default
# `watchdog_timeout_s` (2.0s) so a stalled read unblocks and this thread has
# already reconnected before the watchdog would otherwise need to notice
# anything and ask the monitor thread to intervene.
# OPEN_TIMEOUT_S bounds a connection attempt to a dead or unreachable host;
# it is longer than the read timeout because a fresh TCP+RTSP handshake
# legitimately takes longer than steady-state packet arrival. It is also
# kept comfortably under `CameraReceiver.stop()`'s default 5.0s join
# timeout: a receiver blocked inside `av.open()` against a dead host must
# still be able to notice `_stop_event` and exit within one `stop()` call,
# not merely within one open-timeout cycle plus loop overhead, or `stop()`
# would report `False` (and the client would log a "did not stop cleanly"
# warning) on essentially every teardown that happens to catch a receiver
# mid-connect.
OPEN_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 1.0


def default_opener(url: str, options: dict[str, str]) -> Any:
    import av
    return av.open(url, options=options,
                   timeout=(OPEN_TIMEOUT_S, READ_TIMEOUT_S))


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
        self._reconnect_requested = threading.Event()

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the thread to stop and wait up to `timeout` seconds for it.

        Returns True if the thread actually exited within `timeout`, False
        if the join timed out and the thread is still running — for example
        blocked inside `av.open()`'s own open timeout while connecting to an
        unresponsive host, or inside `decode()` until its read timeout
        elapses. On a False return the thread keeps running in the
        background; a caller that cares (Task 8's monitor, say) can act on
        that, but nothing requires it to.

        This never touches the container: only the thread running `run()`
        may open, read from or close it (see `run()` and `force_reconnect()`
        for why).
        """
        self._stop_event.set()
        self.join(timeout=timeout)
        if self.is_alive():
            log.warning(
                "%s: receiver thread did not stop within %.1fs; it is "
                "still running in the background (likely blocked inside "
                "a connect or decode call whose own timeout has not yet "
                "elapsed)",
                self.camera_id, timeout)
            return False
        return True

    def force_reconnect(self) -> None:
        """Ask the consume loop to abandon its container and reconnect.

        Sets a flag that `_consume` checks once per decoded frame — this
        only helps a container that is actively producing frames. It cannot
        unblock a read that has stopped producing anything at all, because
        there is no iteration on which to check a flag; that case is instead
        bounded by the read timeout passed to `av.open()` (`READ_TIMEOUT_S`),
        so a genuinely wedged read aborts itself without anyone closing it.

        Closing a container from any thread other than the one running its
        `decode()` loop is a use-after-free against a real AVFormatContext,
        so this deliberately never touches the container itself.
        """
        log.warning("%s: forcing reconnect", self.camera_id)
        self._reconnect_requested.set()

    def _close(self, container: Any) -> None:
        """Close `container`. Only ever called from `run()`, i.e. only ever
        by the thread that opened and has been reading this container."""
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

            self._reconnect_requested.clear()
            try:
                self._consume(container)
            except Exception as exc:
                self.last_error = str(exc)
                log.info("%s: stream ended: %s", self.camera_id, exc)
            finally:
                self._close(container)

            if not self._stop_event.is_set():
                self.reconnects += 1
                self._wait(self.backoff.next_delay())

    def _consume(self, container: Any) -> None:
        start_realtime, time_base = _sender_clock(container)
        for decoded in container.decode(video=0):
            if self._stop_event.is_set() or self._reconnect_requested.is_set():
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
