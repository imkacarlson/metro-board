"""Tests for the transfer-decision half of metro_api.

The headline case is test_phantom_connection_regression: it reproduces the
Summer 2026 symptom (two Glenmont rows both showing an orange connection, so no
skip) against the old parsing rule, then shows the fixed rule catching it.
"""
import time

import pytest

from metro_api import DEST_GLENMONT, LINE_RD, MetroApi

CLARENDON_OFFSET = 12  # config['osv_pred_source'] — Clarendon → Metro Center
DUPONT_TO_MC = 4       # ride_dupont_to_mc + walk_transfer


def _legacy_safe_int(val):
    """The pre-fix parser: every non-numeric Min collapsed to 0."""
    try:
        return int(val)
    except ValueError:
        return 0


def _east_preds(rows, parse):
    """Mimic metro_api's Clarendon branch: parse Min, add the offset, sort."""
    preds = []
    for line, mins in rows:
        value = parse(mins)
        if value is None:
            continue
        preds.append((line, value + CLARENDON_OFFSET))
    preds.sort(key=lambda p: p[1])
    return preds


def _glenmont(*minutes):
    return [
        {'line_color': MetroApi._get_line_color(LINE_RD), 'destination': DEST_GLENMONT,
         'arrival': str(m), 'skip_mode': False, 'skip_reason': None}
        for m in minutes
    ]


# --------------------------------------------------------------------------
# _parse_min
# --------------------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('0', 0),
    ('7', 7),
    ('12', 12),
    ('BRD', 0),
    ('ARR', 0),
    ('---', None),
    ('DLY', None),
    ('', None),
    (None, None),
])
def test_parse_min(raw, expected):
    assert MetroApi._parse_min(raw) == expected


def test_parse_min_distinguishes_boarding_from_unknown():
    """The whole bug in one assertion: BRD is a train, '---' is not."""
    assert MetroApi._parse_min('BRD') == 0
    assert MetroApi._parse_min('---') is None


# --------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------

# Clarendon eastbound board: one row with no estimate, two real trains.
CLARENDON_ROWS = [('OR', '---'), ('OR', '9'), ('OR', '12')]


def test_phantom_connection_regression():
    """Old parser: two Glenmont trains, two oranges, no skip. New parser: skip."""
    legacy_preds = _east_preds(CLARENDON_ROWS, _legacy_safe_int)
    assert legacy_preds[0] == ('OR', 12), 'the --- row should become a phantom here'

    legacy_trains = _glenmont(8, 13)
    MetroApi._apply_transfer_logic(legacy_trains, legacy_preds)
    assert [t['skip_mode'] for t in legacy_trains] == [False, False]

    fixed_preds = _east_preds(CLARENDON_ROWS, MetroApi._parse_min)
    assert fixed_preds == [('OR', 21), ('OR', 24)], 'the --- row carries no time'

    fixed_trains = _glenmont(8, 13)
    MetroApi._apply_transfer_logic(fixed_trains, fixed_preds)
    assert fixed_trains[0]['skip_mode'] is True
    assert fixed_trains[0]['skip_reason'] == 'efficiency'
    assert fixed_trains[1]['skip_mode'] is False


# --------------------------------------------------------------------------
# The skip rule
# --------------------------------------------------------------------------

def test_skip_when_next_train_catches_the_same_eastbound():
    trains = _glenmont(8, 11)
    MetroApi._apply_transfer_logic(trains, [('OR', 20)])
    assert trains[0]['skip_mode'] is True
    assert trains[0]['skip_reason'] == 'efficiency'


def test_take_when_next_train_costs_you_a_later_eastbound():
    # ETAs 12 and 21 → connections at 14 and 25, nine minutes apart.
    trains = _glenmont(8, 17)
    MetroApi._apply_transfer_logic(trains, [('OR', 14), ('BL', 25)])
    assert trains[0]['skip_mode'] is False
    assert trains[1]['skip_mode'] is False


# gap 0 — both trains on the literal same departure — is covered by
# test_skip_when_next_train_catches_the_same_eastbound.
@pytest.mark.parametrize('gap, should_skip', [(1, True), (2, True), (3, False)])
def test_skip_tolerance_boundary(gap, should_skip):
    """SKIP_TOLERANCE is 2: a gap of exactly 2 still skips, 3 does not."""
    # Arrivals 8 and 11 → ETAs 12 and 15 at Metro Center, so the first train
    # catches the 14 and the second catches the 14+gap.
    trains = _glenmont(8, 11)
    MetroApi._apply_transfer_logic(trains, [('OR', 14), ('OR', 14 + gap)])
    assert trains[0]['skip_mode'] is should_skip


def test_no_connection_marks_no_data():
    trains = _glenmont(8)
    MetroApi._apply_transfer_logic(trains, [])
    assert trains[0]['skip_mode'] is True
    assert trains[0]['skip_reason'] == 'no_data'


def test_last_train_has_no_successor_so_it_is_never_an_efficiency_skip():
    trains = _glenmont(8)
    MetroApi._apply_transfer_logic(trains, [('OR', 20)])
    assert trains[0]['skip_mode'] is False


def test_third_row_still_gets_a_successor():
    """Step 3: logic runs over the full list, so row 3 can be compared to row 4."""
    trains = _glenmont(8, 12, 16, 19)
    # Rows 3 and 4 (ETAs 20 and 23) both catch the 24.
    MetroApi._apply_transfer_logic(trains, [('OR', 14), ('OR', 24)])
    assert trains[2]['skip_mode'] is True
    assert trains[2]['skip_reason'] == 'efficiency'


def test_take_recolours_to_the_connecting_line():
    trains = _glenmont(8, 30)
    MetroApi._apply_transfer_logic(trains, [('BL', 14), ('OR', 40)])
    assert trains[0]['line_color'] == MetroApi._get_line_color('BL')


def test_non_glenmont_rows_are_untouched():
    trains = _glenmont(8) + [{'line_color': 0, 'destination': 'Shady Gro',
                              'arrival': '9', 'skip_mode': False, 'skip_reason': None}]
    MetroApi._apply_transfer_logic(trains, [('OR', 20)])
    assert trains[1]['skip_mode'] is False
    assert trains[1]['line_color'] == 0


# --------------------------------------------------------------------------
# _overlay_measured
# --------------------------------------------------------------------------

def test_overlay_with_no_measured_data_returns_the_model():
    assert MetroApi._overlay_measured([8, 14, 21], [], 3) == [8, 14, 21]


def test_overlay_full_overlap_prefers_measured():
    assert MetroApi._overlay_measured([8, 14, 21], [7, 15, 20], 3) == [7, 15, 20]


def test_overlay_partial_overlap_keeps_unmatched_model_values():
    # 14 has no measured partner within 3 minutes; 8 and 21 do.
    assert MetroApi._overlay_measured([8, 14, 21], [9, 22], 3) == [9, 14, 22]


def test_overlay_can_pull_a_train_under_the_seven_minute_cut():
    assert MetroApi._overlay_measured([8], [6], 3) == [6]


def test_overlay_does_not_reuse_one_measurement_for_two_trains():
    assert MetroApi._overlay_measured([8, 9], [8], 3) == [8, 9]


def test_overlay_ignores_measurements_outside_the_window():
    assert MetroApi._overlay_measured([20], [3], 3) == [20]


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cache():
    yield
    MetroApi._last_display_data = None
    MetroApi._last_data_time = 0


def test_cold_start_failure_raises():
    from metro_api import MetroApiOnFireException

    MetroApi._last_display_data = None
    with pytest.raises(MetroApiOnFireException):
        MetroApi._fallback_display()


def test_recent_data_is_served_as_is():
    trains = _glenmont(8)
    trains[0]['skip_mode'] = True
    MetroApi._last_display_data = trains
    MetroApi._last_data_time = time.monotonic()

    assert MetroApi._fallback_display() is trains
    assert trains[0]['skip_mode'] is True


def test_stale_data_drops_every_time_based_claim():
    trains = _glenmont(8)
    trains[0]['skip_mode'] = True
    trains[0]['skip_reason'] = 'efficiency'
    trains[0]['line_color'] = MetroApi._get_line_color('OR')
    MetroApi._last_display_data = trains
    MetroApi._last_data_time = time.monotonic() - 200

    out = MetroApi._fallback_display()
    assert out[0]['arrival'] == '---'
    assert out[0]['skip_mode'] is False
    assert out[0]['skip_reason'] is None
    assert out[0]['line_color'] == MetroApi._get_line_color(LINE_RD)
