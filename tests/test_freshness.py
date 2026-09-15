import threading
import time

from anycam2rtsp.frame import Frame
from anycam2rtsp.freshness import LatestFrameBuffer


def frame(n):
    return Frame(camera_id="cam0", frame_id=n, pts=n * 3000,
                 recv_ns=n, decoded_ns=n, image=None)


def test_get_returns_newest_frame():
    buf = LatestFrameBuffer()
    for i in range(5):
        buf.put(frame(i))
    assert buf.get().frame_id == 4


def test_depth_never_exceeds_one():
    buf = LatestFrameBuffer()
    for i in range(100):
        buf.put(frame(i))
        assert buf.depth <= 1


def test_drop_count_matches_overwrites():
    buf = LatestFrameBuffer()
    for i in range(10):
        buf.put(frame(i))
    # 10 put, 9 overwritten, 1 retained
    assert buf.accepted == 10
    assert buf.dropped == 9


def test_take_consumes_the_frame():
    buf = LatestFrameBuffer()
    buf.put(frame(1))
    assert buf.take().frame_id == 1
    assert buf.take() is None
    assert buf.get() is None


def test_consumed_frame_is_not_counted_as_dropped():
    buf = LatestFrameBuffer()
    buf.put(frame(1))
    buf.take()
    buf.put(frame(2))
    assert buf.dropped == 0


def test_empty_buffer_returns_none():
    assert LatestFrameBuffer().get() is None


def test_producer_outrunning_consumer_always_yields_newest():
    buf = LatestFrameBuffer()
    seen = []
    stop = threading.Event()
    barrier = threading.Barrier(2)  # Ensure both threads start near-simultaneously

    def produce():
        barrier.wait()  # Wait for consumer to be ready
        for i in range(2000):
            buf.put(frame(i))
            if i % 10 == 0:  # Yield occasionally to allow consumer thread to run
                time.sleep(0)
        stop.set()

    def consume():
        barrier.wait()  # Wait for producer to be ready
        while not stop.is_set():
            got = buf.take()
            if got is not None:
                seen.append(got.frame_id)

    t_producer = threading.Thread(target=produce)
    t_consumer = threading.Thread(target=consume)
    t_producer.start()
    t_consumer.start()
    t_producer.join()
    t_consumer.join()

    # Verify non-vacuous execution: consumer must have consumed some frames
    assert len(seen) > 100, f"consumer must execute meaningfully; saw {len(seen)} frames"
    assert seen == sorted(seen), "frames must never be delivered out of order"
    assert buf.accepted == 2000
    assert buf.accepted - buf.dropped == len(seen) + buf.depth
