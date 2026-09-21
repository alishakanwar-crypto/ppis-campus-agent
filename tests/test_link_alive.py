import os
import time

import pytest

import link_alive


@pytest.fixture(autouse=True)
def stamp_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        link_alive, "LINK_FILE", tmp_path / ".locks" / "cloud_link.alive"
    )
    monkeypatch.setattr(link_alive, "_last_write", 0.0)
    yield


def test_the_stamp_is_written_where_an_outside_watcher_can_read_it():
    link_alive.note_link_alive()

    assert link_alive.LINK_FILE.exists()
    assert link_alive.seconds_since_link_seen() < 5


def test_a_link_never_seen_reads_as_unknown_rather_than_fresh():
    assert link_alive.seconds_since_link_seen() is None


def test_a_busy_link_does_not_beat_the_disk_on_every_frame(monkeypatch):
    link_alive.note_link_alive()
    first = os.path.getmtime(link_alive.LINK_FILE)
    old = first - 3600
    os.utime(link_alive.LINK_FILE, (old, old))

    link_alive.note_link_alive()

    assert os.path.getmtime(link_alive.LINK_FILE) == pytest.approx(old)


def test_a_link_alive_again_after_the_gap_is_stamped_again(monkeypatch):
    link_alive.note_link_alive()
    old = os.path.getmtime(link_alive.LINK_FILE) - 3600
    os.utime(link_alive.LINK_FILE, (old, old))
    monkeypatch.setattr(
        link_alive,
        "_last_write",
        time.monotonic() - link_alive.WRITE_EVERY_SECONDS - 1,
    )

    link_alive.note_link_alive()

    assert os.path.getmtime(link_alive.LINK_FILE) > old


def test_a_stamp_that_cannot_be_written_never_costs_a_photo(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(link_alive.Path, "write_text", refuse)

    link_alive.note_link_alive()  # must not raise
