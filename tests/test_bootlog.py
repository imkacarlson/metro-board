"""Tests for the on-board log.

bootlog is never OTA-updatable — it ships by cable only — so a mistake here
costs a trip to the board. It imports cleanly on CPython, and pointing
LOG_FILE at tmp_path keeps the real /board.log out of it.
"""
import time

import pytest

import bootlog


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / 'board.log'
    monkeypatch.setattr(bootlog, 'LOG_FILE', str(path))
    monkeypatch.setattr(bootlog, 'OLD_LOG_FILE', str(path) + '.1')
    bootlog._pending = []
    yield path
    bootlog._pending = []


def _clock(monkeypatch, year, hour=7, minute=3, second=9):
    monkeypatch.setattr(
        time, 'localtime',
        lambda *a: (year, 8, 23, hour, minute, second, 4, 235, -1))


def test_every_appended_line_is_stamped(log, monkeypatch):
    _clock(monkeypatch, 2026)
    bootlog.append('code: button=True')
    assert log.read_text() == '2026-08-23 07:03:09 code: button=True\n'


def test_a_deferred_line_keeps_its_record_time_not_the_flush_time(log, monkeypatch):
    """The whole point of defer() is recording when each import completed."""
    _clock(monkeypatch, 2026, second=1)
    bootlog.defer('code: config ok')
    _clock(monkeypatch, 2026, second=4)
    bootlog.defer('code: display up')
    _clock(monkeypatch, 2026, minute=30, second=0)
    bootlog.flush()

    lines = log.read_text().splitlines()
    assert lines[0].startswith('2026-08-23 07:03:01 ')
    assert lines[1].startswith('2026-08-23 07:03:04 ')


def test_a_pre_ntp_clock_is_marked(log, monkeypatch):
    """CircuitPython starts at 2000-01-01; a reader must not read that as real."""
    _clock(monkeypatch, 2000)
    bootlog.append('code: importing')
    assert log.read_text() == '~07:03:09 code: importing\n'


def test_rotation_preserves_a_generation_instead_of_deleting(log, monkeypatch):
    _clock(monkeypatch, 2026)
    log.write_text('x' * (bootlog.MAX_BYTES + 1))
    bootlog.append('code: after the wrap')

    old = log.parent / 'board.log.1'
    assert old.read_text() == 'x' * (bootlog.MAX_BYTES + 1)
    assert 'code: after the wrap' in log.read_text()


def test_a_second_rotation_replaces_the_kept_generation(log, monkeypatch):
    _clock(monkeypatch, 2026)
    log.write_text('first' + 'x' * bootlog.MAX_BYTES)
    bootlog.append('cycle one')
    log.write_text('second' + 'x' * bootlog.MAX_BYTES)
    bootlog.append('cycle two')

    assert (log.parent / 'board.log.1').read_text().startswith('second')
    assert 'cycle two' in log.read_text()


def test_a_write_failure_is_still_swallowed(log, monkeypatch, tmp_path):
    """Logging must never become the thing that breaks the board."""
    monkeypatch.setattr(bootlog, 'LOG_FILE', str(tmp_path / 'nope' / 'board.log'))
    bootlog.append('this cannot be written anywhere')  # must not raise


def test_a_broken_clock_does_not_lose_the_line(log, monkeypatch):
    def boom(*a):
        raise RuntimeError('no RTC')

    monkeypatch.setattr(time, 'localtime', boom)
    bootlog.append('code: exception in main loop')
    assert log.read_text() == 'code: exception in main loop\n'
