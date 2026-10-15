"""When the clock sync and the nightly update reboot are allowed to happen.

The 08:00 weekday reload came from a 3-hourly clock sync whose phase the
no-service gap reset to 05:00, landing the next one mid-commute. These pin the
replacement rule: sync on an unset clock, otherwise only in 2-5 AM, at most
once per SYNC_INTERVAL.
"""
import time

import pytest

import metro_api


def _at(hour, year=2026):
    # Thursday, so the weekday transfer window would apply.
    return time.struct_time((year, 10, 15, hour, 0, 0, 3, 288, -1))


@pytest.fixture
def clock(monkeypatch):
    state = {'local': _at(12), 'mono': 1000.0, 'syncs': 0}
    monkeypatch.setattr(time, 'localtime', lambda *a: state['local'])
    monkeypatch.setattr(time, 'monotonic', lambda: state['mono'])
    monkeypatch.setattr(metro_api, '_next_sync', None)

    def fake_sync():
        state['syncs'] += 1
        metro_api._next_sync = state['mono'] + metro_api.SYNC_INTERVAL

    monkeypatch.setattr(metro_api, 'update_system_time', fake_sync)
    return state


def test_unset_clock_syncs_at_any_hour(clock):
    clock['local'] = _at(8, year=2000)
    metro_api.sync_time_if_due()
    assert clock['syncs'] == 1


@pytest.mark.parametrize('hour', [5, 6, 7, 8, 12, 23, 0, 1])
def test_set_clock_never_syncs_outside_quiet_hours(clock, hour):
    # Includes 08:00, the hour that kept rebooting the board.
    clock['local'] = _at(hour)
    metro_api.sync_time_if_due()
    assert clock['syncs'] == 0


def test_quiet_hours_sync_once_per_night(clock):
    clock['local'] = _at(2)
    metro_api.sync_time_if_due()
    clock['mono'] += 5
    metro_api.sync_time_if_due()
    assert clock['syncs'] == 1

    clock['mono'] += 24 * 60 * 60
    metro_api.sync_time_if_due()
    assert clock['syncs'] == 2


def test_quiet_hours_need_a_set_clock(clock):
    # 02:00 on an unset clock is really "two hours after power-on".
    clock['local'] = _at(2, year=2000)
    assert not metro_api.is_quiet_hours()
    clock['local'] = _at(2)
    assert metro_api.is_quiet_hours()
    clock['local'] = _at(5)
    assert not metro_api.is_quiet_hours()
