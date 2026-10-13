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

def _overlay(modeled, measured, window=3):
    """_overlay_measured in plain minutes. Model rows are labelled 'm', measured
    'd', so each result also says which source it came from."""
    out = MetroApi._overlay_measured([('m', x) for x in modeled],
                                     [('d', x) for x in measured], window)
    return [(src, x) for src, x in out]


def test_overlay_with_no_measured_data_returns_the_model():
    assert _overlay([8, 14, 21], []) == [('m', 8), ('m', 14), ('m', 21)]


def test_overlay_full_overlap_prefers_measured():
    assert _overlay([8, 14, 21], [7, 15, 20]) == [('d', 7), ('d', 15), ('d', 20)]


def test_overlay_partial_overlap_keeps_unmatched_model_values():
    # 14 has no measured partner within 3 minutes; 8 and 21 do.
    assert _overlay([8, 14, 21], [9, 22]) == [('d', 9), ('m', 14), ('d', 22)]


def test_overlay_can_pull_a_train_under_the_display_floor():
    assert _overlay([8], [5]) == [('d', 5)]


def test_overlay_does_not_reuse_one_measurement_for_two_trains():
    assert _overlay([8, 9], [8]) == [('d', 8), ('m', 9)]


def test_overlay_keeps_a_measured_train_with_no_model_partner():
    """A measurement outside every window is its own train, not noise."""
    assert _overlay([20], [3]) == [('d', 3), ('m', 20)]


def test_overlay_keeps_the_train_that_has_already_left_tenleytown():
    """The bug: Tenleytown's front train is boarding (model 8); the one ahead of
    it has left Tenleytown and Dupont shows it at 6. Greedy matching paired the
    6 with the model's 8, so the boarding train lost its row and the board
    showed one train where there were two."""
    assert _overlay([8, 15], [6, 8, 15]) == [('d', 6), ('d', 8), ('d', 15)]


def test_overlay_with_an_empty_model_is_dupont_alone():
    assert _overlay([], [6, 12]) == [('d', 6), ('d', 12)]


def test_overlay_takes_dupont_s_destination_label():
    out = MetroApi._overlay_measured([('Glenmont', 8), ('Glenmont', 12)],
                                     [('SilvrSpg', 8), ('Glenmont', 12)], 3)
    assert out == [('SilvrSpg', 8), ('Glenmont', 12)]


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

from metro_api import keep_transfer_row


def _row(loc, line, dest, mins, group):
    return (loc, line, dest, mins, group)  # parse_train_fragment's row shape


@pytest.fixture
def _fake_api(monkeypatch):
    """Drive fetch_train_predictions off a canned response, no network."""
    def install(rows):
        monkeypatch.setattr(metro_api, 'is_transfer_intelligence_time', lambda: True)
        monkeypatch.setattr(metro_api, '_init_network', lambda: None)
        monkeypatch.setattr(metro_api, 'optimized_fetch',
                            lambda path, keep=None: list(rows))
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


def test_full_fetch_shows_a_six_minute_train_that_has_left_tenleytown(_fake_api):
    """Tenleytown's board no longer has this train; Dupont's does, at 6."""
    _fake_api([
        _row('A03', 'RD', 'Glenmont', '6', SOUTH),
        _row('A07', 'RD', 'Glenmont', '4', SOUTH),        # → 12 at Dupont
        _row('A03', 'RD', 'Glenmont', '12', SOUTH),
        _row('K02', 'OR', 'New Carrollton', '2', '1'),    # → 14 at Metro Center
    ])
    out = MetroApi.fetch_train_predictions('A03')
    assert [(t['destination'], t['arrival']) for t in out] == [('Glenmont', '6'), ('Glenmont', '12')]


def test_full_fetch_floor_is_six_minutes(_fake_api):
    _fake_api([
        _row('A03', 'RD', 'Shady Grove', '5', NORTH),
        _row('A03', 'RD', 'Shady Grove', '6', NORTH),
    ])
    out = MetroApi.fetch_train_predictions('A03')
    assert [t['arrival'] for t in out] == ['6']


@pytest.mark.parametrize('dest', ['Largo', 'Downtown Largo', 'Dwntn Largo',
                                  'New Carrollton', 'N Carrollton', 'NewCrlton'])
def test_eastbound_destinations_survive_renames_and_abbreviation(dest):
    assert metro_api.is_east_dest(dest) is True
    assert keep_transfer_row('C07', 'BL', dest) is True


@pytest.mark.parametrize('dest', ['Franconia', 'Vienna', 'Ashburn', 'Huntington'])
def test_westbound_destinations_are_not_connections(dest):
    assert metro_api.is_east_dest(dest) is False


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


# --------------------------------------------------------------------------
# The wire parser: optimized_fetch → direct_chunk_parse_trains →
# parse_train_fragment → extract_field
#
# These are characterization tests first: every one of them is written against
# keep=None, so they pass identically before and after the parse-time filter
# exists. That is what makes the equivalence group below mean something.
# --------------------------------------------------------------------------

import gc as _gc

from metro_api import (direct_chunk_parse_trains, extract_field,
                       keep_simple_row, keep_transfer_row,
                       parse_train_fragment)


def _train_json(loc, line, dest, mins, group, car='8', dest_code='B11',
                loc_name='Dupont Circle'):
    """A *full* WMATA train object, in WMATA's real key order.

    The parser is a substring scanner, so a five-key fixture would not exercise
    what can actually break: DestinationCode/DestinationName sit right next to
    Destination, and LocationName right next to LocationCode.
    """
    return (
        '{"Car":"%s","Destination":"%s","DestinationCode":"%s",'
        '"DestinationName":"%s","Group":"%s","Line":"%s","LocationCode":"%s",'
        '"LocationName":"%s","Min":"%s"}'
        % (car, dest, dest_code, dest, group, line, loc, loc_name, mins)
    )


def _wmata_body(rows):
    return '{"Trains":[' + ','.join(rows) + ']}'


class _FakeResponse:
    """Feeds a body through iter_content() in fixed-size chunks."""

    def __init__(self, body, chunk_size=256, encode=True):
        self._data = body.encode('utf-8') if encode else body
        self._chunk_size = chunk_size
        self.closed = False

    def iter_content(self, _n=256):
        step = self._chunk_size
        for i in range(0, len(self._data), step):
            yield self._data[i:i + step]

    def close(self):
        self.closed = True


ALL_CHUNK_SIZES = [1, 2, 3, 7, 16, 64, 256, 4096]

# A realistic five-station body: two upstream eastbound sources, Metro Center,
# Tenleytown, Dupont.
FOUR_STATION_ROWS = [
    _train_json('K02', 'OR', 'New Carrollton', '2', '1', loc_name='Clarendon'),
    _train_json('K02', 'SV', 'Largo', '6', '1', loc_name='Clarendon'),
    _train_json('K02', 'OR', 'Vienna', '4', '2', loc_name='Clarendon'),
    _train_json('K02', 'SV', 'No Passenger', '3', '2', loc_name='Clarendon'),
    _train_json('C07', 'BL', 'Largo', '5', '1', loc_name='Pentagon'),
    _train_json('C07', 'YL', 'Greenbelt', '3', '1', loc_name='Pentagon'),
    _train_json('C07', 'BL', 'Franconia', '8', '2', loc_name='Pentagon'),
    _train_json('C01', 'OR', 'New Carrollton', '3', '1', loc_name='Metro Center'),
    _train_json('C01', 'BL', 'Downtown Largo', 'BRD', '1', loc_name='Metro Center'),
    _train_json('C01', 'SV', 'Ashburn', '2', '2', loc_name='Metro Center'),
    _train_json('C01', 'OR', 'No Passenger', '5', '1', loc_name='Metro Center'),
    _train_json('A07', 'RD', 'Glenmont', '4', '1', loc_name='Tenleytown'),
    _train_json('A07', 'RD', 'Silver Spring', '9', '1', loc_name='Tenleytown'),
    _train_json('A07', 'RD', 'Shady Grove', '3', '2', loc_name='Tenleytown'),
    _train_json('A07', 'RD', 'Farragut North', '6', '1', loc_name='Tenleytown'),
    _train_json('A03', 'RD', 'Glenmont', '12', '1'),
    _train_json('A03', 'RD', 'Shady Grove', '9', '2'),
    _train_json('A03', 'RD', 'No Passenger', '2', '2'),
]


def _legacy_relevant(row):
    """An independent transcription of the elif chain in
    fetch_train_predictions — deliberately *not* sharing code with
    keep_transfer_row, so predicate/branch drift shows up as a failure.

    Route identity only, matching the chain's station/line/destination
    conditions; time and direction are the chain's business, not the filter's.
    """
    loc, line, dest = row[0], row[1], row[2]
    east = ('New Carrollton', 'N Carrollton', 'NewCrlton', 'Largo', 'Downtown Largo')
    if loc == 'K02':
        return line in ('OR', 'SV') and dest in east
    if loc == 'C07':
        return line == 'BL' and dest in east
    if loc == 'C01':
        return line in ('OR', 'SV', 'BL') and dest in east
    if loc == 'A07':
        return line == 'RD'
    if loc == 'A03':
        return line == 'RD'
    return False


# --- extraction ------------------------------------------------------------

def test_extract_field_is_not_confused_by_neighbouring_keys():
    frag = _train_json('A03', 'RD', 'Glenmont', '5', '1')
    assert extract_field(frag, '"Destination":"') == 'Glenmont'
    assert extract_field(frag, '"LocationCode":"') == 'A03'
    assert extract_field(frag, '"Line":"') == 'RD'
    assert extract_field(frag, '"Min":"') == '5'
    assert extract_field(frag, '"Group":"') == '1'


def test_extract_field_missing_key_is_none():
    assert extract_field('{"Line":"RD"}', '"Nope":"') is None


def test_parse_single_station_body():
    body = _wmata_body([_train_json('A03', 'RD', 'Glenmont', '5', '1'),
                        _train_json('A03', 'RD', 'Shady Grove', 'BRD', '2')])
    trains = direct_chunk_parse_trains(_FakeResponse(body))
    assert trains == [
        ('A03', 'RD', 'Glenmont', '5', '1'),
        ('A03', 'RD', 'Shady Grove', 'BRD', '2'),
    ]


@pytest.mark.parametrize('chunk_size', ALL_CHUNK_SIZES)
def test_output_is_identical_at_every_chunk_size(chunk_size):
    """Subsumes every hand-picked split: inside "Trains":[, inside a field
    value, mid-object, mid-key. One byte at a time is the hardest case."""
    body = _wmata_body(FOUR_STATION_ROWS)
    reference = direct_chunk_parse_trains(_FakeResponse(body, 4096))
    assert direct_chunk_parse_trains(_FakeResponse(body, chunk_size)) == reference
    assert len(reference) == len(FOUR_STATION_ROWS)


def test_split_inside_the_trains_marker():
    body = _wmata_body([_train_json('A03', 'RD', 'Glenmont', '5', '1')])
    cut = body.index('"Trains":[') + 4
    chunks = [body[:cut].encode(), body[cut:].encode()]

    class _R:
        def iter_content(self, _n=256):
            return iter(chunks)

    assert direct_chunk_parse_trains(_R())[0][2] == 'Glenmont'


def test_split_inside_a_field_value():
    body = _wmata_body([_train_json('A03', 'RD', 'Silver Spring', '5', '1')])
    cut = body.index('Silver Spring') + 6
    chunks = [body[:cut].encode(), body[cut:].encode()]

    class _R:
        def iter_content(self, _n=256):
            return iter(chunks)

    assert direct_chunk_parse_trains(_R())[0][2] == 'Silver Spring'


@pytest.mark.parametrize('raw', ['BRD', 'ARR', '---', '4'])
def test_min_survives_the_parser_verbatim(raw):
    """The parser must not interpret Min — that is _parse_min's job, later."""
    body = _wmata_body([_train_json('A03', 'RD', 'Glenmont', raw, '1')])
    assert direct_chunk_parse_trains(_FakeResponse(body))[0][3] == raw


def test_empty_trains_array():
    assert direct_chunk_parse_trains(_FakeResponse('{"Trains":[]}')) == []


def test_body_with_no_trains_key():
    assert direct_chunk_parse_trains(_FakeResponse('{"Error":"nope"}')) == []


def test_stream_ending_mid_object_drops_the_partial_row():
    body = _wmata_body([_train_json('A03', 'RD', 'Glenmont', '5', '1'),
                        _train_json('A03', 'RD', 'Shady Grove', '9', '2')])
    truncated = body[:len(body) - 30]
    trains = direct_chunk_parse_trains(_FakeResponse(truncated))
    assert [t[2] for t in trains] == ['Glenmont']


def test_undecodable_bytes_do_not_kill_the_parse():
    body = _wmata_body([_train_json('A03', 'RD', 'Glenmont', '5', '1')])
    raw = body.encode('utf-8').replace(b'Glenmont', b'Glen\xffont')
    trains = direct_chunk_parse_trains(_FakeResponse(raw, 4096, encode=False))
    assert len(trains) == 1
    assert trains[0][0] == 'A03'


def test_raw_count_cap_bounds_the_loop():
    """The runaway guard has to count rows *seen*: with a filter in place a
    retained-only cap would leave the loop unbounded over the stream."""
    rows = [_train_json('A03', 'RD', 'Glenmont', '5', '1')] * (metro_api.RAW_MAX + 40)
    trains = direct_chunk_parse_trains(_FakeResponse(_wmata_body(rows)),
                                       lambda loc, line, dest: False)
    assert trains == []
    assert metro_api._last_raw_count == metro_api.RAW_MAX


def test_retained_count_cap_bounds_the_list():
    rows = [_train_json('A03', 'RD', 'Glenmont', '5', '1')] * (metro_api.KEEP_MAX + 10)
    trains = direct_chunk_parse_trains(_FakeResponse(_wmata_body(rows)))
    assert len(trains) == metro_api.KEEP_MAX


def test_a_malformed_fragment_is_dropped_before_keep_is_consulted():
    """Today's semantics: a row missing Line/Destination/LocationCode never
    reaches the predicate, so a predicate cannot resurrect it."""
    calls = []

    def spy(loc, line, dest):
        calls.append((loc, line, dest))
        return True

    assert parse_train_fragment('{"Line":"RD","Min":"5"}', spy) is None
    assert calls == []


# --- filter equivalence ----------------------------------------------------

def test_filtering_during_the_parse_matches_filtering_afterwards():
    """The invariant the whole change rests on: the predicate is a strict
    over-approximation of what the downstream elif chain keeps."""
    body = _wmata_body(FOUR_STATION_ROWS)
    unfiltered = direct_chunk_parse_trains(_FakeResponse(body))
    filtered = direct_chunk_parse_trains(_FakeResponse(body), keep_transfer_row)
    assert [r for r in unfiltered if _legacy_relevant(r)] == filtered


@pytest.mark.parametrize('chunk_size', ALL_CHUNK_SIZES)
def test_equivalence_holds_at_every_chunk_size(chunk_size):
    body = _wmata_body(FOUR_STATION_ROWS)
    unfiltered = direct_chunk_parse_trains(_FakeResponse(body, chunk_size))
    filtered = direct_chunk_parse_trains(_FakeResponse(body, chunk_size),
                                         keep_transfer_row)
    assert [r for r in unfiltered if _legacy_relevant(r)] == filtered


def test_the_filter_materially_shrinks_retention():
    body = _wmata_body(FOUR_STATION_ROWS)
    unfiltered = direct_chunk_parse_trains(_FakeResponse(body))
    filtered = direct_chunk_parse_trains(_FakeResponse(body), keep_transfer_row)
    assert len(filtered) < len(unfiltered)


@pytest.mark.parametrize('loc, line, dest, kept', [
    ('K02', 'OR', 'New Carrollton', True),
    ('K02', 'SV', 'Largo', True),
    ('K02', 'OR', 'Vienna', False),          # westbound
    ('K02', 'BL', 'Largo', False),           # wrong line for this station
    ('C07', 'BL', 'NewCrlton', True),
    ('C07', 'YL', 'Greenbelt', False),
    ('C07', 'BL', 'Franconia', False),
    ('A07', 'RD', 'Glenmont', True),
    ('A07', 'RD', 'Shady Grove', True),      # northbound: the A07 g= diagnostic
    ('A07', 'OR', 'Vienna', False),
    ('A03', 'RD', 'Glenmont', True),
    ('A03', 'RD', 'Shady Grove', True),      # both Groups
    ('A03', 'BL', 'Largo', False),
    ('C01', 'OR', 'New Carrollton', True),
    ('C01', 'BL', 'Downtown Largo', True),
    ('C01', 'SV', 'Ashburn', False),         # westbound
    ('C01', 'RD', 'Glenmont', False),        # Red is A01, not C01
    ('B35', 'RD', 'Glenmont', False),        # a station we never asked for
])
def test_keep_transfer_row_accept_reject_table(loc, line, dest, kept):
    assert keep_transfer_row(loc, line, dest) is kept


@pytest.mark.parametrize('dest, kept', [
    ('Glenmont', True), ('Shady Grove', True),
    ('No Passenger', False), ('NoPssenger', False),
    ('ssenger', False), ('No Psngr', False),
])
def test_keep_simple_row_drops_only_non_passenger(dest, kept):
    assert keep_simple_row('A03', 'RD', dest) is kept


# --- end to end, over the wire --------------------------------------------

@pytest.fixture
def _wire_api(monkeypatch):
    """Run fetch_train_predictions against a real wire body: no shortcut past
    optimized_fetch, so the parse path under test is the one that runs."""
    def install(body, chunk_size=64):
        monkeypatch.setattr(metro_api, 'is_transfer_intelligence_time', lambda: True)
        monkeypatch.setattr(metro_api, '_init_network', lambda: None)
        monkeypatch.setattr(metro_api, 'log_memory', lambda label: 20000)
        monkeypatch.setattr(_gc, 'mem_free', lambda: 100000, raising=False)
        monkeypatch.setattr(_gc, 'mem_alloc', lambda: 10000, raising=False)

        class _Net:
            def fetch(self, url, headers=None):
                return _FakeResponse(body, chunk_size)

        monkeypatch.setattr(metro_api, '_network', _Net())
        metro_api._prev_front_t = None
        metro_api._obs.clear()
    yield install
    metro_api._prev_front_t = None
    metro_api._obs.clear()
    metro_api._network = None


def test_end_to_end_display_is_byte_identical_with_and_without_the_filter(
        _wire_api, monkeypatch):
    body = _wmata_body(FOUR_STATION_ROWS)
    _wire_api(body)

    monkeypatch.setattr(metro_api, 'keep_transfer_row', None)
    unfiltered = MetroApi.fetch_train_predictions('A03')
    monkeypatch.undo()

    _wire_api(body)
    filtered = MetroApi.fetch_train_predictions('A03')
    assert filtered == unfiltered
    assert filtered, 'the fixture should produce a non-empty board'


def test_a_station_code_disagreeing_with_config_disables_the_filter(_wire_api,
                                                                    capsys):
    """Safety valve: a caller passing a station config does not know about
    would otherwise have every one of its rows rejected and blank the board."""
    rows = [_train_json('B35', 'RD', 'Glenmont', '12', '1'),
            _train_json('B35', 'RD', 'Shady Grove', '9', '2')]
    _wire_api(_wmata_body(rows))
    out = MetroApi.fetch_train_predictions('B35')
    # With the filter wrongly enabled every B35 row would be rejected during the
    # parse and the board would come up blank. Falling back to keep=None
    # degrades to today's behaviour instead. Both directions show: the main
    # station's own southbound row is a measured train in its own right.
    assert [t['destination'] for t in out] == ['Shady Gro', 'Glenmont']
    assert 'kept 2 raw 2' in capsys.readouterr().out


def test_a07_keeps_every_direction_through_the_parse(_wire_api):
    """A07 rows are kept regardless of Group: direction is accept_southbound's
    decision downstream, and the filter must never pre-empt it."""
    rows = [_train_json('A07', 'RD', 'Shady Grove', '3', '2'),
            _train_json('A07', 'RD', 'Glenmont', '4', '1')]
    body = _wmata_body(rows)
    kept = direct_chunk_parse_trains(_FakeResponse(body), keep_transfer_row)
    assert [r[2] for r in kept] == ['Shady Grove', 'Glenmont']

    _wire_api(body)
    out = MetroApi.fetch_train_predictions('A03')
    # ...and only the southbound one reaches the display: 4 + 8 = 12.
    assert [(t['destination'], t['arrival']) for t in out] == [('Glenmont', '12')]


# --------------------------------------------------------------------------
# Missing fields must not invent trains
# --------------------------------------------------------------------------

# A real WMATA row with no estimate: Min is unquoted null, so extract_field's
# '"Min":"' prefix does not match and it returns None.
NULL_MIN_ROW = ('{"Car":"8","Destination":"Glenmont","DestinationCode":"B11",'
                '"DestinationName":"Glenmont","Group":"1","Line":"RD",'
                '"LocationCode":"A03","LocationName":"Dupont Circle","Min":null}')

NO_GROUP_ROW = ('{"Destination":"Glenmont","Line":"RD","LocationCode":"A03",'
                '"Min":"9"}')


def test_a_null_min_carries_no_time_instead_of_arriving_now():
    parsed = parse_train_fragment(NULL_MIN_ROW)
    assert parsed[3] == '---'
    assert MetroApi._parse_min(parsed[3]) is None


def test_a_missing_group_matches_neither_direction():
    parsed = parse_train_fragment(NO_GROUP_ROW)
    assert parsed[4] == '0'
    assert parsed[4] not in (metro_api.NORTHBOUND_GROUP,
                             metro_api.SOUTHBOUND_GROUP)
    assert accept_southbound(parsed[4], 'Glenmont') is False


def test_a_row_with_no_group_does_not_silently_become_southbound(_wire_api):
    """It used to: a Dupont row with no Group defaulted to southbound and
    overwrote the modelled minute with a measurement of a train that may not
    even be going that way."""
    rows = [_train_json('A07', 'RD', 'Glenmont', '4', '1', loc_name='Tenleytown'),
            NO_GROUP_ROW]
    _wire_api(_wmata_body(rows))
    out = MetroApi.fetch_train_predictions('A03')
    # Tenleytown 4 + 8 = 12, modelled. The Group-less 9 is not a measurement.
    assert [(t['destination'], t['arrival']) for t in out] == [('Glenmont', '12')]


# --------------------------------------------------------------------------
# Metro Center's own eastbound board
# --------------------------------------------------------------------------

def test_merge_east_without_metro_center_is_the_model():
    model = [('OR', 14), ('BL', 18)]
    assert MetroApi._merge_east(model, [], 3) == model


def test_merge_east_adds_connections_already_past_the_upstream_stations():
    """The blind spot: an Orange at Metro Center in 10 has left Clarendon, so
    the model cannot have it. Metro Center's own board can."""
    out = MetroApi._merge_east([('OR', 18)], [('OR', 10), ('OR', 17)], 3)
    assert out == [('OR', 10), ('OR', 17)]


def test_merge_east_matches_within_a_line_only():
    """A Silver measured at 13 is not the Orange modelled at 14."""
    out = MetroApi._merge_east([('OR', 14)], [('SV', 13)], 3)
    assert out == [('SV', 13), ('OR', 14)]


def test_full_fetch_front_train_catches_the_connection_it_used_to_miss(_fake_api):
    """Southbound at 6 → Metro Center at 10. The real first connection is an
    Orange at 11, already past Clarendon. Without C01 the train would chase the
    Silver at 19, find the next southbound catches it too, and wrongly blink
    SKIP."""
    _fake_api([
        _row('A03', 'RD', 'Glenmont', '6', SOUTH),
        _row('A03', 'RD', 'Glenmont', '12', SOUTH),
        _row('C01', 'OR', 'New Carrollton', '11', '1'),
        _row('K02', 'SV', 'Largo', '7', '1'),             # → 19 at Metro Center
    ])
    out = MetroApi.fetch_train_predictions('A03')
    assert out[0]['arrival'] == '6'
    assert out[0]['skip_mode'] is False
    assert out[0]['line_color'] == MetroApi._get_line_color('OR')


def test_full_fetch_requests_metro_center_in_the_same_call(monkeypatch, _fake_api):
    _fake_api([_row('A03', 'RD', 'Glenmont', '9', SOUTH)])
    paths = []
    monkeypatch.setattr(metro_api, 'optimized_fetch',
                        lambda path, keep=None: paths.append(path) or
                        [_row('A03', 'RD', 'Glenmont', '9', SOUTH)])
    MetroApi.fetch_train_predictions('A03')
    assert len(paths) == 1
    assert paths[0].split(',') == ['K02', 'C07', 'C01', 'A07', 'A03']


# --------------------------------------------------------------------------
# Memory shape
# --------------------------------------------------------------------------

def test_parsed_rows_are_tuples_not_dicts():
    """Up to KEEP_MAX rows are alive at the tightest point of a refresh; a
    tuple is a fraction of a five-key dict."""
    row = parse_train_fragment(_train_json('A03', 'RD', 'Glenmont', '5', '1'))
    assert type(row) is tuple and len(row) == 5


def test_parser_handles_a_nested_object_without_cutting_it_short():
    body = ('{"Trains":[{"Line":"RD","Destination":"Glenmont","Extra":{"a":"b"},'
            '"LocationCode":"A03","Min":"5","Group":"1"}]}')
    for size in ALL_CHUNK_SIZES:
        trains = direct_chunk_parse_trains(_FakeResponse(body, size))
        assert trains == [('A03', 'RD', 'Glenmont', '5', '1')], size
