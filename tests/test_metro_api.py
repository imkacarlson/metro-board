"""Tests for the transfer-decision half of metro_api.

The headline case is test_phantom_connection_regression: it reproduces the
Summer 2026 symptom (two Glenmont rows both showing an orange connection, so no
skip) against the old parsing rule, then shows the fixed rule catching it.
"""
import time

import pytest

import metro_api
from metro_api import (LINE_RD, MetroApi, accept_southbound,
                       implied_offset, normalize_destination,
                       reaches_metro_center, record_observation)

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
        {'line_color': MetroApi._get_line_color(LINE_RD), 'destination': 'Glenmont',
         'arrival': str(m), 'skip_mode': False, 'skip_reason': None, 'southbound': True}
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
# Southbound acceptance: direction, not destination
# --------------------------------------------------------------------------

SOUTH, NORTH = '1', '2'


@pytest.mark.parametrize('dest', [
    'Glenmont', 'Silver Spring', 'NoMa-Gallaudet', 'Union Station',
    'Metro Center',  # terminating *at* the destination is fine
])
def test_southbound_accepted_by_group(dest):
    assert accept_southbound(SOUTH, dest) is True


def test_metro_center_turnback_accepted_farragut_turnback_rejected():
    """The case that separates "southbound" from "actually useful"."""
    assert accept_southbound(SOUTH, 'Metro Center') is True
    assert accept_southbound(SOUTH, 'Farragut North') is False


@pytest.mark.parametrize('dest', ['Farragut No', 'FarragutN', 'Farragut Nor'])
def test_abbreviated_farragut_still_rejected(dest):
    """Substring match, not equality — WMATA abbreviates unpredictably."""
    assert accept_southbound(SOUTH, dest) is False


def test_dupont_turnback_rejected():
    assert accept_southbound(SOUTH, 'Dupont Circle') is False


def test_non_passenger_rejected():
    assert accept_southbound(SOUTH, 'No Passenger') is False
    assert accept_southbound(SOUTH, 'NoPssenger') is False


def test_inverted_group_mapping_yields_an_empty_southbound_list():
    """If Group→direction were backwards at Tenleytown, nothing gets through —
    an empty board is loud; northbound trains in southbound rows are silent."""
    northbound_rows = [('Shady Grove', NORTH), ('Shady Grv', NORTH),
                       ('Friendship Heights', NORTH), ('Grosvenor', NORTH),
                       ('Medical Center', NORTH), ('Bethesda', NORTH)]
    # Wrong mapping: read Group 2 as if it were southbound.
    assert [d for d, _ in northbound_rows if reaches_metro_center(d)] == []
    # Right mapping: rejected on Group alone.
    assert [d for d, g in northbound_rows if accept_southbound(g, d)] == []


@pytest.mark.parametrize('raw, expected', [
    ('Silver Spring', 'SilvrSpg'),
    ('NoMa-Gallaudet', 'NoMa'),
    ('Union Station', 'UnionStn'),
    ('Glenmont', 'Glenmont'),
])
def test_southbound_destinations_survive_the_eight_character_slice(raw, expected):
    """train_board slices to 8; without normalising these truncate mid-word."""
    out = normalize_destination(raw)
    assert out == expected
    assert len(out) <= 8


def test_shady_grove_normalisation_is_unchanged():
    assert normalize_destination('Shady Grove') == 'Shady Gro'
    assert normalize_destination('Shady Grv') == 'Shady Gro'


def test_a_silver_spring_train_keeps_the_transfer_colouring():
    trains = _glenmont(8, 30)
    trains[0]['destination'] = 'SilvrSpg'
    MetroApi._apply_transfer_logic(trains, [('BL', 14), ('OR', 40)])
    assert trains[0]['skip_mode'] is False
    assert trains[0]['line_color'] == MetroApi._get_line_color('BL')


# --------------------------------------------------------------------------
# BRD vs ARR upstream
# --------------------------------------------------------------------------

def test_arr_models_one_minute_later_than_brd_upstream():
    from metro_api import upstream_min

    assert upstream_min('BRD') == 0
    assert upstream_min('ARR') == 1
    assert upstream_min('4') == 4
    assert upstream_min('---') is None


def test_dupont_own_board_still_treats_brd_and_arr_alike():
    """_parse_min is untouched: at your own platform both mean "board it"."""
    assert MetroApi._parse_min('BRD') == 0
    assert MetroApi._parse_min('ARR') == 0


# --------------------------------------------------------------------------
# The travel-time measurement (observe-only)
# --------------------------------------------------------------------------

@pytest.fixture
def _reset_obs():
    metro_api._obs.clear()
    metro_api._prev_front_t = None
    yield
    metro_api._obs.clear()
    metro_api._prev_front_t = None


def test_implied_offset_is_measured_minus_modeled_input():
    # Tenleytown says 4, Dupont measures the same train at 12 → 8 minutes.
    assert implied_offset(4, [12], 8, 3) == 8


def test_implied_offset_does_not_depend_on_the_current_offset():
    """The arithmetic never reads the offset, so it cannot feed back on itself.

    A configured 7 and a configured 8 both pair with the same row and both
    report the travel time that was actually observed.
    """
    assert implied_offset(4, [12], 7, 3) == 8
    assert implied_offset(4, [12], 8, 3) == 8
    assert implied_offset(4, [12], 9, 3) == 8


def test_implied_offset_picks_the_nearest_measurement():
    assert implied_offset(4, [10, 13, 25], 8, 3) == 9  # 13 is nearest to 12


def test_implied_offset_none_when_nothing_matches():
    assert implied_offset(4, [30], 8, 3) is None
    assert implied_offset(4, [], 8, 3) is None


def test_one_observation_per_train_not_one_per_refresh(_reset_obs):
    """At a 5 s refresh a naive sampler would log ~60 copies of one train."""
    # Train walks in from 9 minutes out, then sits at the trigger for a while.
    recorded = [record_observation(9, [17])]
    recorded.append(record_observation(5, [13]))
    recorded += [record_observation(4, [12]) for _ in range(60)]

    assert [r for r in recorded if r is not None] == [8]
    assert metro_api._obs == [8]


def test_each_new_train_contributes_its_own_observation(_reset_obs):
    for _ in range(3):
        record_observation(9, [17])   # next train appears, above the trigger
        record_observation(4, [12])   # ...and crosses it
    assert metro_api._obs == [8, 8, 8]


def test_observation_buffer_is_capped(_reset_obs):
    for _ in range(10):
        record_observation(9, [17])
        record_observation(4, [12])
    assert len(metro_api._obs) == metro_api.OBS_KEEP


def test_a_train_with_no_dupont_match_records_nothing(_reset_obs):
    record_observation(9, [])
    assert record_observation(4, []) is None
    assert metro_api._obs == []


def test_one_anomalous_train_does_not_move_the_median(_reset_obs):
    """The failure that motivated this: one weird train dominating the estimate.

    11 is as far out as an observation can get — the match window rejects
    anything wilder as a different train — and the median still reads 8.
    """
    for implied in (8, 8, 11, 8, 8):
        record_observation(9, [])
        record_observation(4, [4 + implied])
    assert metro_api._obs == [8, 8, 11, 8, 8]
    assert sorted(metro_api._obs)[len(metro_api._obs) // 2] == 8


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


# --------------------------------------------------------------------------
# The parse loop, end to end
# --------------------------------------------------------------------------

def _row(loc, line, dest, mins, group):
    return {'LocationCode': loc, 'Line': line, 'Destination': dest,
            'Min': mins, 'Group': group}


@pytest.fixture
def _fake_api(monkeypatch):
    """Drive fetch_train_predictions off a canned response, no network."""
    def install(rows):
        monkeypatch.setattr(metro_api, 'is_transfer_intelligence_time', lambda: True)
        monkeypatch.setattr(metro_api, '_init_network', lambda: None)
        monkeypatch.setattr(metro_api, 'optimized_fetch', lambda path: list(rows))
        monkeypatch.setattr(metro_api, 'log_memory', lambda label: 20000)
        metro_api._prev_front_t = None
        metro_api._obs.clear()
    yield install
    metro_api._prev_front_t = None
    metro_api._obs.clear()


def test_full_fetch_keeps_a_silver_spring_train_as_a_southbound_row(_fake_api):
    _fake_api([
        _row('A07', 'RD', 'Silver Spring', '2', SOUTH),   # → 10 at Dupont
        _row('A07', 'RD', 'Glenmont', '8', SOUTH),        # → 16 at Dupont
        _row('A07', 'RD', 'Farragut North', '4', SOUTH),  # turnback, dropped
        _row('A07', 'RD', 'Shady Grove', '3', NORTH),     # wrong way, dropped
        _row('A03', 'RD', 'Shady Grove', '9', NORTH),
        _row('K02', 'OR', 'New Carrollton', '2', '1'),    # → 14 at Metro Center
    ])
    out = MetroApi.fetch_train_predictions('A03')

    south = [t for t in out if t['southbound']]
    assert [(t['destination'], t['arrival']) for t in south] == [('SilvrSpg', '10'), ('Glenmont', '16')]
    assert 'Farragut North' not in [t['destination'] for t in out]

    north = [t for t in out if not t['southbound']]
    assert north[0]['destination'] == 'Shady Gro'

    # The Silver Spring train transfers like any other southbound train.
    assert south[0]['skip_mode'] is False
    assert south[0]['line_color'] == MetroApi._get_line_color('OR')


def test_full_fetch_drops_everything_if_the_group_mapping_were_inverted(_fake_api):
    _fake_api([
        _row('A07', 'RD', 'Shady Grove', '2', NORTH),
        _row('A07', 'RD', 'Grosvenor', '5', NORTH),
    ])
    out = MetroApi.fetch_train_predictions('A03')
    assert out == []


def test_full_fetch_records_one_observation_when_the_front_train_crosses(_fake_api):
    rows = [_row('A07', 'RD', 'Glenmont', '9', SOUTH),
            _row('A03', 'RD', 'Glenmont', '17', SOUTH)]
    _fake_api(rows)
    MetroApi.fetch_train_predictions('A03')
    assert metro_api._obs == []

    rows[:] = [_row('A07', 'RD', 'Glenmont', '4', SOUTH),
               _row('A03', 'RD', 'Glenmont', '12', SOUTH)]
    for _ in range(12):  # a minute of 5-second refreshes on the same train
        MetroApi.fetch_train_predictions('A03')
    assert metro_api._obs == [8]
