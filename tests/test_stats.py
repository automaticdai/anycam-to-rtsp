from anycam.stats import StreamStats


def test_format_line_includes_the_silent_failure_signals():
    line = StreamStats(camera_id="cam0", frames=900, dropped=612,
                       reconnects=2, age_s=0.033, depth=1,
                       last_error=None).format_line()
    assert "cam0" in line
    assert "900" in line
    assert "612" in line
    assert "2" in line


def test_stalled_stream_is_reported_as_stalled():
    stats = StreamStats(camera_id="cam0", frames=10, dropped=0, reconnects=0,
                        age_s=9.5, depth=0, last_error=None)
    assert stats.is_stalled(timeout_s=2.0)
    assert "STALLED" in stats.format_line(timeout_s=2.0)


def test_healthy_stream_is_not_stalled():
    stats = StreamStats(camera_id="cam0", frames=10, dropped=0, reconnects=0,
                        age_s=0.02, depth=1, last_error=None)
    assert not stats.is_stalled(timeout_s=2.0)
    assert "STALLED" not in stats.format_line(timeout_s=2.0)
