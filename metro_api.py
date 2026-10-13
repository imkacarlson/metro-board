from config import config, dim_color
from secrets import secrets
from rtc import RTC

# Convert config dict to constants to avoid dict lookups and reduce stack depth
METRO_API_URL = config['metro_api_url']
METRO_API_KEY = config['metro_api_key']  
METRO_API_RETRIES = config['metro_api_retries']
OSV_PRED_SOURCE = config['osv_pred_source']
BL_PRED_SOURCE = config['bl_pred_source']
RD_GLEN_PRED_SOURCE = config['rd_glen_pred_source']
RD_GLEN_OFFSET = RD_GLEN_PRED_SOURCE[1]
DISPLAY_FLOOR_MIN = config['display_floor_min']
WALK_TRANSFER = config['walk_transfer']
RIDE_DUPONT_TO_MC = config['ride_dupont_to_mc']
SKIP_TOLERANCE = config['skip_tolerance']
DUPONT_MATCH_WINDOW = config['dupont_match_window']
EAST_MATCH_WINDOW = config['east_match_window']
LOADING_MIN_TEXT = config['loading_min_text']
NUM_TRAINS = config['num_trains']
TRANSFER_START_HOUR = config['transfer_start_hour']
TRANSFER_START_MINUTE = config['transfer_start_minute']
TRANSFER_END_HOUR = config['transfer_end_hour']
TRANSFER_END_MINUTE = config['transfer_end_minute']

MAIN_STATION = config['metro_station_code']
OSV_STATION = OSV_PRED_SOURCE[0]
BL_STATION = BL_PRED_SOURCE[0]
RD_GLEN_STATION = RD_GLEN_PRED_SOURCE[0]
MC_EAST_STATION = config['mc_east_station']

# Delete config dict after extracting constants
del config

# Fail loudly and early rather than emitting 401s forever. The key comes from
# settings.toml (see config.py); a missing key means settings.toml was never
# filled in on this board.
if not METRO_API_KEY:
    raise RuntimeError('WMATA_API_KEY missing — set it in settings.toml')

# At Dupont Circle (A03) the WMATA "Group" field is the direction:
#   Group 1 -> southbound (Glenmont / Silver Spring)
#   Group 2 -> northbound (Shady Grove, or whatever it short-turns at)
# Selecting on Group rather than on a destination string is what keeps the
# northbound row alive across service changes: during the 2025 summer shutdown
# these trains terminate at Friendship Heights, not Shady Grove.
NORTHBOUND_GROUP = '2'
SOUTHBOUND_GROUP = '1'

# The point below which a fetch is not attempted at all: the board reloads
# instead, on the theory that fragmentation is unrecoverable in place.
#
# This was 10000, which measurement showed sits *inside* the normal operating
# band. One HTTPS fetch permanently claims ~54 KB — free goes 64 K -> 10 K
# across the first refresh and never comes back — so steady state is ~10.8 K
# with the parse filter in place, and every ordinary fluctuation was tripping a
# reboot. 23 consecutive cycles of that are in /board.log.1.
#
# 7000 keeps the net for genuine exhaustion while leaving ~3.8 KB of ordinary
# headroom. Note that a fetch which fails outright is *not* fatal:
# optimized_fetch returns [] and the caller falls back to the last good board,
# so this reload is a precaution, not the thing standing between us and a crash.
MEM_FLOOR = 7000

# How long the last good board stays believable once refreshes start failing.
# 90 s is ~18 missed refreshes at the 5 s interval — past that we stop claiming
# to know anything about connections.
STALE_AFTER = 90

# Optimized networking globals
_network = None
_request_count = 0

# Time syncing globals
_last_sync_time = 0
_sync_interval = 3 * 60 * 60  # 3 hours in seconds

def _init_network():
    """Initialize network with memory optimizations"""
    global _network
    if _network is None:
        import board
        import neopixel
        from adafruit_matrixportal.network import Network
        
        # Turn off the status NeoPixel completely
        status_pixel = neopixel.NeoPixel(board.NEOPIXEL, 1)
        status_pixel[0] = (0, 0, 0)
        del status_pixel
        
        # Create network object
        _network = Network(status_neopixel=None)
        
        # Clean up imports
        del board, neopixel, Network
        
        import gc
        gc.collect()
        print("Optimized networking initialized")

def update_system_time():
    """Update system clock date/time from worldtimeapi.org. Sets RTC() time and
       returns current local time struct and success status.
    """
    import time
    global _network, _last_sync_time
    try:
        _init_network()
        print("⏰ Syncing time with worldtimeapi.org...")
        
        # Get time from worldtimeapi.org (free, no account needed)
        local_time_string = _network.get_local_time()
        elements = local_time_string.split(" ")
        
        # Update our last sync time
        _last_sync_time = time.time()
        
        print(f"⏰ Time synced successfully: {local_time_string}")
        return RTC().datetime, True
        
    except Exception as e:
        print(f"⏰ Time sync failed: {e}")
        # Push sync time ahead to retry in 30 minutes (don't overwhelm server)
        _last_sync_time = time.time() - _sync_interval + (30 * 60)
        return time.localtime(), False

def optimized_fetch(api_path, keep=None):
    """Ultra-memory-efficient fetch with direct chunk parsing - no string assembly

    `keep(loc, line, dest)` decides which rows are worth a dict at all. Passing
    None retains everything, which is what every caller used to do.
    """
    global _network
    import gc
    
    global _request_count
    _request_count += 1

    _init_network()
    
    # PROVEN TECHNIQUE: Multiple gc.collect() calls before large allocation
    # Each call frees more "phantom" objects from previous network operations
    for i in range(4):
        gc.collect()
    
    # Check memory after cleanup
    free_before = gc.mem_free()
    if free_before < MEM_FLOOR:
        print(f"⚠️ Memory fragmentation detected ({free_before} bytes free)")
        print("🔄 Performing emergency memory reset...")
        # The reload used to leave nothing behind: 34 boot cycles were logged
        # with zero exception lines, because this path never wrote one. req= is
        # the load-bearing field — free_before is measured *before* the fetch,
        # so a reload at req=1 would mean the shortfall is boot-time baseline
        # (matrix, TLS, font) rather than peak retention during the parse.
        import bootlog
        bootlog.append('fetch: reload, free=%d req=%d xfer=%s'
                       % (free_before, _request_count, is_transfer_intelligence_time()))
        # NUCLEAR OPTION: supervisor.reload() to completely reset memory
        import supervisor
        supervisor.reload()
    
    try:
        # Build full URL
        api_url = f"{METRO_API_URL}{api_path}"
        
        # Make request
        response = _network.fetch(api_url, headers={'api_key': METRO_API_KEY})
        # REVOLUTIONARY APPROACH: Parse directly from chunks without building full string
        trains = direct_chunk_parse_trains(response, keep)
        
        # Close response immediately after parsing
        response.close() 
        del response
        gc.collect()
        log_memory("After direct chunk parsing")
        
        return trains
        
    except (RuntimeError, TimeoutError, OSError) as e:
        # Network trouble is the caller's business: swallowing it here is what
        # made METRO_API_RETRIES a no-op and MetroApiOnFireException unreachable.
        print(f"Optimized fetch network error: {e}")
        gc.collect()
        raise

    except Exception as e:
        print(f"Optimized fetch failed: {e}")
        gc.collect()
        return []

_osv_preds = []   # Clarendon / Courthouse  OR/SV
_bl_preds  = []   # Pentagon / Arlington   BL
_rd_glen_preds = []   # (destination, arrival mins) @Dupont southbound, modelled from Tenleytown
_rd_south_measured = []  # (destination, arrival mins) @Dupont southbound, off Dupont's own board
_rd_north_preds = []  # (destination, arrival mins) @Dupont for northbound trains
_mc_measured = []     # (line, arrival mins) eastbound, off Metro Center's own board

# Live measurement of the Tenleytown → Dupont travel time. Every refresh holds
# a modelled and a measured figure for the same train; their difference is the
# real travel time, free, at no extra API cost. Observe-only for now: logged,
# never applied. Phase 2 turns it into the offset itself.
_obs = []             # implied travel times, oldest first
_prev_front_t = None  # previous refresh's front Tenleytown Min

OBS_TRIGGER = 4  # observe when the front Tenleytown train crosses this
OBS_KEEP = 5     # samples kept; small ints, ~100 bytes

# Memory tracking globals
_max_mem_seen = 0
_min_mem_seen = 999999

class MetroApiOnFireException(Exception):
    pass

def is_transfer_intelligence_time():
    """Ultra-lightweight time check for weekday morning commute hours (6:45-8:30 AM)"""
    import time
    t = time.localtime()
    weekday = t[6]  # 0=Monday, 6=Sunday
    hour, minute = t[3], t[4]
    # Weekend check (Saturday=5, Sunday=6)
    if weekday > 4:
        return False
    
    # Time window check (6:45 AM - 8:30 AM)
    if hour < TRANSFER_START_HOUR or hour > TRANSFER_END_HOUR:
        return False
    if hour == TRANSFER_START_HOUR and minute < TRANSFER_START_MINUTE:
        return False
    if hour == TRANSFER_END_HOUR and minute > TRANSFER_END_MINUTE:
        return False
    
    return True

# String constants to reduce memory allocations
DEST_NEW_CARROLLTON = 'New Carrollton'
DEST_N_CARROLLTON = 'N Carrollton'
DEST_NEWCRLTON = 'NewCrlton'
DEST_LARGO = 'Largo'
DEST_SHADY_GROVE = 'Shady Grove'
DEST_SHADY_GRV = 'Shady Grv'  # API sometimes returns abbreviated form
DEST_SHADY_GRO = 'Shady Gro'  # Pre-truncated for display
DEST_NO_PASSENGER = 'No Passenger'
DEST_NOPSSENGER = 'NoPssenger'
DEST_SSENGER = 'ssenger'
DEST_NO_PSNGR = 'No Psngr'

# Line code constants
LINE_RD = 'RD'
LINE_OR = 'OR'
LINE_SV = 'SV'
LINE_BL = 'BL'
LINE_YL = 'YL'
LINE_GR = 'GR'

# Destinations that mean "you cannot board this train"
NON_PASSENGER = (DEST_NO_PASSENGER, DEST_NOPSSENGER, DEST_SSENGER, DEST_NO_PSNGR)

# Eastbound out of Metro Center: the connection we are trying to catch. One
# spelling of this set, shared by the parse-time filter and the branches it
# mirrors, so the two cannot drift apart.
#
# Substrings, not exact names, for the same reason as NOT_VIA_METRO_CENTER:
# Largo was renamed Downtown Largo, and an exact 'Largo' match would silently
# drop every Blue and Silver connection the day the sign text follows suit —
# the skip logic would then run on Orange alone and still look plausible.
EAST_DESTS = ('arroll', 'Crlton', 'argo')

# True if this destination is an eastbound connection out of Metro Center.
def is_east_dest(dest):
    for frag in EAST_DESTS:
        if frag in dest:
            return True
    return False

# Southbound destinations that never reach Metro Center, as substrings.
# Substrings because WMATA abbreviates unpredictably — see the four spellings
# of "No Passenger" and three of "Shady Grove" above.
#
# Going south from Dupont (A03) there is exactly one station before Metro
# Center (A01): Farragut North (A02). So Farragut North and Dupont itself are
# the *complete* set of southbound turnbacks short of the destination — that
# is geography, not guesswork. A train terminating at Metro Center is fine.
#
# The rest are northbound terminals. They are here because Group→direction is
# only proven at Dupont, not at Tenleytown: if that mapping is inverted the
# southbound list goes empty, which is loud, instead of quietly northbound.
NOT_VIA_METRO_CENTER = ('arragut', 'upont', 'hady', 'riendship',
                        'rosvenor', 'edical', 'ethesda')

# True if a southbound train with this destination gets you to Metro Center.
def reaches_metro_center(dest):
    for frag in NOT_VIA_METRO_CENTER:
        if frag in dest:
            return False
    return True

# True if this row is a train you can ride from here to Metro Center.
def accept_southbound(group, dest):
    return (group == SOUTHBOUND_GROUP and dest not in NON_PASSENGER
            and reaches_metro_center(dest))

# Minutes for a *modelled* upstream train. _parse_min collapses BRD and ARR to
# 0, which is right for a train at your own platform — either way you can board
# it. Upstream they are about a minute apart: BRD is leaving now, ARR has its
# dwell still to serve.
def upstream_min(raw):
    mins = MetroApi._parse_min(raw)
    if mins == 0 and raw == 'ARR':
        return 1
    return mins

# Shorten known long destination names so they survive display truncation:
# train_board slices to 8 characters, so anything longer truncates mid-word.
def normalize_destination(dest):
    if dest in (DEST_SHADY_GROVE, DEST_SHADY_GRV):
        return DEST_SHADY_GRO
    if 'ilver' in dest:
        return 'SilvrSpg'
    if 'oMa' in dest:
        return 'NoMa'
    if 'nion S' in dest:
        return 'UnionStn'
    return dest

# Observed Tenleytown → Dupont travel time for one train, or None. `offset` only
# decides *which* measured row is the same train; the number returned is
# measured − t_min, which never touches it. That is what stops the estimate
# feeding back on itself.
def implied_offset(t_min, measured, offset, window):
    expected = t_min + offset
    best = None
    for m in measured:
        d = abs(m - expected)
        if d <= window and (best is None or d < abs(best - expected)):
            best = m
    return None if best is None else best - t_min


# Sample the travel time at most once per Tenleytown train. At a 5 s refresh a
# naive sampler collects ~60 copies of one train and lets a single weird train
# dominate. Triggering on the front train's Min *crossing* OBS_TRIGGER fires
# once: Min falls monotonically for a given train, and the next train's Min
# jumps back above the threshold.
def record_observation(front_t, measured):
    global _prev_front_t
    prev = _prev_front_t
    _prev_front_t = front_t

    if front_t is None or prev is None or prev <= OBS_TRIGGER or front_t > OBS_TRIGGER:
        return None

    implied = implied_offset(front_t, measured, RD_GLEN_OFFSET, DUPONT_MATCH_WINDOW)
    if implied is None:
        return None

    _obs.append(implied)
    if len(_obs) > OBS_KEEP:
        del _obs[0]
    s = sorted(_obs)
    print(f"OFFSET_OBS t={front_t} d={front_t + implied} implied={implied} median={s[len(s) // 2]} n={len(s)}")
    return implied


def log_memory(label):
    """Collect, report free bytes to serial, and track the low-water mark."""
    import gc
    global _max_mem_seen, _min_mem_seen

    gc.collect()
    free = gc.mem_free()
    if free > _max_mem_seen:
        _max_mem_seen = free
    if free < _min_mem_seen:
        _min_mem_seen = free

    print(label, free, 'free, low', _min_mem_seen)
    return free

# Two different jobs, so two different counters. `train_count >= 50` used to be
# both the memory bound and the runaway bound; once most rows are filtered out
# a retained-only cap would leave the loop unbounded over the stream.
RAW_MAX = 240   # every {...} seen: the real runaway guard
KEEP_MAX = 40   # retained rows: a ceiling, not a target — expect 12-18

_last_raw_count = 0  # rows seen by the last parse, for the ✅ Cached: line


def direct_chunk_parse_trains(response, keep=None):
    """Stream the Trains array out of HTTP chunks, one object at a time.

    Each object is located with str.find and cut out with a single slice. The
    old loop grew it with `current_train += char`, which is one fresh string
    per byte: ~250 allocations of rising size per row, over up to RAW_MAX rows,
    every refresh — about the worst thing you can do to a heap whose failure
    mode is fragmentation.

    Rows come back as tuples; see parse_train_fragment.
    """
    import gc
    global _last_raw_count
    trains = []

    buffer = ""
    raw_count = 0
    chunks = 0
    _last_raw_count = 0
    in_trains_array = False

    try:
        for chunk in response.iter_content(256):
            if not chunk:
                continue

            if isinstance(chunk, bytes):
                try:
                    chunk_text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    chunk_text = chunk.decode('utf-8', 'replace')
            else:
                chunk_text = str(chunk)
            buffer += chunk_text
            del chunk_text

            while True:
                if not in_trains_array:
                    trains_pos = buffer.find('"Trains":[')
                    if trains_pos == -1:
                        # Keep only enough tail to catch a split marker
                        if len(buffer) > 50:
                            buffer = buffer[-50:]
                        break
                    in_trains_array = True
                    buffer = buffer[trains_pos + 10:]
                    continue

                start = buffer.find('{')
                if start == -1:
                    buffer = ""  # nothing here can begin an object
                    break

                # Find the matching close brace. WMATA train objects are flat,
                # but depth is still tracked so a nested one cannot be cut short.
                depth = 1
                i = start
                end = -1
                while True:
                    close = buffer.find('}', i + 1)
                    if close == -1:
                        break
                    opn = buffer.find('{', i + 1, close)
                    if opn != -1:
                        depth += 1
                        i = opn
                        continue
                    depth -= 1
                    i = close
                    if depth == 0:
                        end = close
                        break

                if end == -1:
                    # Incomplete: keep it, from its opening brace, for the next chunk
                    if start:
                        buffer = buffer[start:]
                    break

                train_data = parse_train_fragment(buffer[start:end + 1], keep)
                buffer = buffer[end + 1:]
                raw_count += 1
                _last_raw_count = raw_count
                if train_data:
                    trains.append(train_data)
                    if len(trains) >= KEEP_MAX:
                        return trains
                if raw_count >= RAW_MAX:  # Prevent runaway parsing
                    return trains

            # Keyed on chunks, not on retained rows: with a filter in place the
            # retained count sits at 0 through most of the stream.
            chunks += 1
            if chunks % 8 == 0:
                gc.collect()

            # Safety: prevent buffer from growing too large
            if len(buffer) > 1000:
                print(f"⚠️ Buffer overflow protection: {len(buffer)} chars")
                buffer = buffer[-500:]

    except Exception as e:
        print(f"Direct chunk parsing error: {e}")
        gc.collect()

    return trains

def parse_train_fragment(train_json, keep=None):
    """One WMATA train object -> (LocationCode, Line, Destination, Min, Group).

    A tuple, not a dict: a five-key dict carries a hash table several times the
    size of a five-slot tuple, and up to KEEP_MAX of these are alive at once at
    the tightest point of the refresh. Callers unpack it in that order.
    """
    try:
        line = extract_field(train_json, '"Line":"')
        dest = extract_field(train_json, '"Destination":"')
        location = extract_field(train_json, '"LocationCode":"')

        # Malformed fragments are dropped before `keep` is consulted — a
        # predicate cannot resurrect one.
        if line and dest and location:
            if keep is not None and not keep(location, line, dest):
                return None
            mins = extract_field(train_json, '"Min":"')
            group = extract_field(train_json, '"Group":"')
            # WMATA emits unquoted "Min":null, for which extract_field returns
            # None. Defaulting that to '0' invented a train arriving now; '---'
            # maps to None in _parse_min, which is the honest answer. Likewise a
            # missing Group used to default to southbound: '0' matches neither
            # direction constant.
            return (location, line, dest, mins or '---', group or '0')
    except:
        pass
    return None

def extract_field(fragment, field_prefix):
    """Extract a single field value from a JSON fragment"""
    start = fragment.find(field_prefix)
    if start == -1:
        return None
    start += len(field_prefix)
    end = fragment.find('"', start)
    if end == -1:
        return None
    return fragment[start:end]

# Off-peak: one station, and the only rows worth a dict are ones you can board.
# Plain module-level function, not a closure — a closure allocates a cell plus a
# function object per refresh, in the hot path.
def keep_simple_row(loc, line, dest):
    return dest not in NON_PASSENGER


def fetch_simple_dupont_trains(station_code: str) -> [dict]:
    """Memory-efficient single-station fetch for non-transfer hours"""
    import gc
    import time
    global _last_sync_time, _sync_interval
    
    # Sync with time server every ~3 hours
    current_time = time.time()
    if current_time - _last_sync_time > _sync_interval:
        update_system_time()
    
    # No ?TrainGroup= — WMATA ignores it. Simple mode deliberately shows both
    # directions merged, which is the long-standing off-peak behaviour.
    api_path = station_code
    
    try:
        all_trains = optimized_fetch(api_path, keep_simple_row)
        if not all_trains:
            print("❌ No train data received from simple API call")
            return []
        
        # Filter to trains DISPLAY_FLOOR_MIN+ minutes away and take top N
        filtered_trains = []
        for _loc, line, dest, raw, _group in all_trains:
            if dest in NON_PASSENGER:
                continue

            mins = MetroApi._parse_min(raw)
            if mins is not None and mins >= DISPLAY_FLOOR_MIN:
                line_color = MetroApi._get_line_color(line)
                dest = normalize_destination(dest)

                filtered_trains.append({
                    'line_color': line_color,
                    'destination': dest,
                    'arrival': str(mins),
                    'skip_mode': False,
                    'skip_reason': None
                })
                
                if len(filtered_trains) >= NUM_TRAINS:
                    break
        
        del all_trains
        gc.collect()
        print(f"🚇 Simple mode: {len(filtered_trains)} trains {DISPLAY_FLOOR_MIN}+ min out")
        return filtered_trains
        
    except Exception as e:
        print(f"Simple fetch failed: {e}")
        gc.collect()
        return []

# Mirrors the station/line/destination clauses of the elif chain in
# fetch_train_predictions below, one clause per branch. Route identity only —
# never Min, never Group — which makes this a strict over-approximation of what
# the chain keeps: it may retain a row the chain later drops, never the reverse.
# That property is what makes filtering during the parse behaviour-preserving,
# and it is the invariant the equivalence test asserts.
def keep_transfer_row(loc, line, dest):
    if loc == OSV_STATION:      # -> the upstream OR/SV branch (Clarendon)
        return line in (LINE_OR, LINE_SV) and is_east_dest(dest)
    if loc == BL_STATION:       # -> the upstream BL branch (Pentagon)
        return line == LINE_BL and is_east_dest(dest)
    if loc == RD_GLEN_STATION:  # -> the Tenleytown branch; direction is
        return line == LINE_RD  #    accept_southbound's business, not ours.
    if loc == MAIN_STATION:     # -> Dupont's own board, both Groups;
        return line == LINE_RD  #    direction is decided downstream.
    if loc == MC_EAST_STATION:  # -> Metro Center's own eastbound board
        return line in (LINE_OR, LINE_SV, LINE_BL) and is_east_dest(dest)
    return False


class MetroApi:
    @staticmethod
    def fetch_train_predictions(station_code: str) -> [dict]:
        import gc
        import time
        global _network, _request_count, _osv_preds, _bl_preds, _rd_glen_preds, _rd_south_measured, _rd_north_preds, _mc_measured
        global _last_sync_time, _sync_interval
        
        # Sync with time server every ~3 hours
        current_time = time.time()
        if current_time - _last_sync_time > _sync_interval:
            update_system_time()
        
        # Check if we should use transfer intelligence or simple mode
        if not is_transfer_intelligence_time():
            return fetch_simple_dupont_trains(station_code)
        
        _init_network()
        gc.collect()

        # Station codes from config
        main_station = station_code
        osv_station, osv_offset = OSV_PRED_SOURCE
        bl_station, bl_offset = BL_PRED_SOURCE
        rd_glen_station, rd_glen_offset = RD_GLEN_PRED_SOURCE

        # Five stations in one request: two upstream eastbound sources,
        # Metro Center's own eastbound board, the Glenmont upstream source, and
        # Dupont itself. One request, not two: each HTTPS fetch is what costs
        # memory here, while a longer body streams through the same small buffer.
        # No ?TrainGroup= — WMATA silently ignores it (verified: identical
        # responses with and without), so direction is decided here instead.
        all_stations = f"{osv_station},{bl_station},{MC_EAST_STATION},{rd_glen_station},{main_station}"
        api_path = all_stations

        # Safety valve: keep_transfer_row is written against the stations in
        # config. A caller passing some other station would have every one of
        # its rows rejected and blank the board, so fall back to retaining
        # everything, which is exactly today's behaviour.
        keep = keep_transfer_row if station_code == MAIN_STATION else None

        for i in range(METRO_API_RETRIES + 1):
            gc.collect()

            try:
                all_trains = optimized_fetch(api_path, keep)
                if not all_trains:
                    print("❌ No train data received from combined API call")
                    return MetroApi._fallback_display()

                # Clear all prediction caches
                _osv_preds.clear()
                _bl_preds.clear()
                _rd_glen_preds.clear()
                _rd_south_measured.clear()
                _rd_north_preds.clear()
                _mc_measured.clear()

                # Process all trains from the single response
                front_t = None  # smallest *numeric* Tenleytown Min this refresh
                for loc, line, dest, raw, group in all_trains:

                    # Upstream OR/SV predictions (Clarendon)
                    if loc == osv_station and line in (LINE_OR, LINE_SV) and is_east_dest(dest):
                        mins = MetroApi._parse_min(raw)
                        if mins is None:
                            continue
                        _osv_preds.append((line, mins + osv_offset))

                    # Upstream BL predictions (Pentagon)
                    elif loc == bl_station and line == LINE_BL and is_east_dest(dest):
                        mins = MetroApi._parse_min(raw)
                        if mins is None:
                            continue
                        _bl_preds.append((line, mins + bl_offset))

                    # Metro Center's own eastbound board: measured, no offset.
                    # Covers the connections already past Clarendon and Pentagon.
                    elif loc == MC_EAST_STATION and line in (LINE_OR, LINE_SV, LINE_BL) and is_east_dest(dest):
                        mins = MetroApi._parse_min(raw)
                        if mins is None:
                            continue
                        _mc_measured.append((line, mins))

                    # Upstream southbound predictions (Tenleytown). Direction is
                    # Group, not a destination string: the ride is only as far as
                    # Metro Center, so Silver Spring and other turnbacks are just
                    # as boardable as Glenmont.
                    elif loc == rd_glen_station and line == LINE_RD:
                        if not accept_southbound(group, dest):
                            continue
                        mins = upstream_min(raw)
                        if mins is None:
                            continue
                        # Numeric Mins only drive the measurement, which sidesteps
                        # the BRD/ARR ambiguity for calibration purposes.
                        if raw.isdigit() and (front_t is None or mins < front_t):
                            front_t = mins
                        if mins + rd_glen_offset >= DISPLAY_FLOOR_MIN:
                            _rd_glen_preds.append((normalize_destination(dest), mins + rd_glen_offset))

                    # Dupont's own southbound board. Same trains as the Tenleytown
                    # model once they are close enough for Dupont to see them, but
                    # measured rather than modelled — see _overlay_measured.
                    elif loc == main_station and line == LINE_RD and group == SOUTHBOUND_GROUP:
                        if not accept_southbound(group, dest):
                            continue
                        mins = MetroApi._parse_min(raw)
                        if mins is not None:
                            _rd_south_measured.append((normalize_destination(dest), mins))

                    # Northbound, straight off Dupont's own board. No destination
                    # filter and no offset — these trains stop here. Group is the
                    # direction; anything you can actually board counts.
                    elif loc == main_station and line == LINE_RD and group == NORTHBOUND_GROUP:
                        if dest in NON_PASSENGER:
                            continue
                        mins = MetroApi._parse_min(raw)
                        if mins is not None and mins >= DISPLAY_FLOOR_MIN:
                            _rd_north_preds.append((normalize_destination(dest), mins))

                n_kept = len(all_trains)
                del all_trains
                gc.collect()

                # Sort Red Line caches by arrival time (earliest first)
                _rd_glen_preds.sort(key=lambda p: p[1])
                _rd_south_measured.sort(key=lambda p: p[1])
                _rd_north_preds.sort(key=lambda p: p[1])

                print('cached osv', len(_osv_preds), 'bl', len(_bl_preds),
                      'mc', len(_mc_measured),
                      'model', len(_rd_glen_preds), 'meas', len(_rd_south_measured),
                      'north', len(_rd_north_preds),
                      'kept', n_kept, 'raw', _last_raw_count)

                record_observation(front_t, [p[1] for p in _rd_south_measured])

                south = MetroApi._overlay_measured(_rd_glen_preds, _rd_south_measured, DUPONT_MATCH_WINDOW)

                # Cut to the display floor *before* building display dicts, so
                # nothing below it ever costs one. The whole remaining list is
                # kept, not just the top N: the third row's SKIP decision needs a
                # successor to compare against, so that cut happens afterwards.
                red = MetroApi._get_line_color(LINE_RD)
                filtered_trains = []
                for dest, m in south:
                    if m >= DISPLAY_FLOOR_MIN:
                        filtered_trains.append({'line_color': red, 'destination': dest, 'arrival': str(m), 'skip_mode': False, 'skip_reason': None, 'southbound': True})
                for dest, m in _rd_north_preds:
                    filtered_trains.append({'line_color': red, 'destination': dest, 'arrival': str(m), 'skip_mode': False, 'skip_reason': None, 'southbound': False})
                del south
                filtered_trains.sort(key=MetroApi._safe_sort_key)

                modeled_east = _osv_preds + _bl_preds
                modeled_east.sort(key=lambda p: p[1])
                _mc_measured.sort(key=lambda p: p[1])
                all_east_preds = MetroApi._merge_east(modeled_east, _mc_measured, EAST_MATCH_WINDOW)
                del modeled_east
                MetroApi._apply_transfer_logic(filtered_trains, all_east_preds)

                display_trains = filtered_trains[:NUM_TRAINS]
                del filtered_trains
                gc.collect()
                MetroApi._last_display_data = display_trains
                MetroApi._last_data_time = time.monotonic()

                del all_east_preds
                gc.collect()
                log_memory("After processing complete")

                return display_trains

            except (RuntimeError, TimeoutError, OSError) as e:
                print(f'Network error: {e}')
                gc.collect()
                if i >= METRO_API_RETRIES:
                    return MetroApi._fallback_display()

    # Set once a refresh succeeds; read by _fallback_display.
    _last_display_data = None
    _last_data_time = 0

    @staticmethod
    def _parse_min(val):
        """Minutes as an int, or None when WMATA has no estimate.

        BRD/ARR are imminent. Anything else (---, DLY, empty) carries no time
        information; returning 0 for it invents a train that does not exist.
        """
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
        return 0 if val in ('BRD', 'ARR') else None

    @staticmethod
    def _fallback_display():
        """The last good board, degraded honestly once it ages out.

        Raises only on a cold start, where there is nothing to show and blanking
        the display is the truthful answer.
        """
        import time
        prior = MetroApi._last_display_data
        if not prior:
            raise MetroApiOnFireException()

        if time.monotonic() - MetroApi._last_data_time < STALE_AFTER:
            return prior

        # Too old to stand behind. Keep the destinations, drop every claim that
        # depends on the clock — especially the connection colour.
        for t in prior:
            t['arrival'] = LOADING_MIN_TEXT
            t['skip_mode'] = False
            t['skip_reason'] = None
            if t.get('southbound'):
                t['line_color'] = MetroApi._get_line_color(LINE_RD)
        return prior

    @staticmethod
    def _overlay_measured(modeled, measured, window):
        """Merge the Tenleytown model with Dupont's own board.

        Both are ascending (destination, minutes) lists. Where one train appears
        in both, Dupont's measured figure and label win; it takes the measured
        row nearest the modelled minute, so a train that has already left
        Tenleytown cannot steal the match from the one now boarding there.

        A measured train with no modelled partner is still a train, and is kept.
        It is almost always one that has already left Tenleytown, so it can never
        appear in the model again. Dropping it — which this used to do — took the
        nearest southbound train off the board from ~8 minutes out, exactly when
        it was still catchable.
        """
        if not measured:
            return modeled

        out = []
        j = 0
        n = len(measured)
        for p in modeled:
            m = p[1]
            while j < n and measured[j][1] < m - window:
                out.append(measured[j])
                j += 1
            best = -1
            k = j
            while k < n and measured[k][1] <= m + window:
                if best < 0 or abs(measured[k][1] - m) < abs(measured[best][1] - m):
                    best = k
                k += 1
            if best < 0:
                out.append(p)
            else:
                # Anything passed over on the way to the nearest match is a
                # separate train, not a duplicate of this one.
                out.extend(measured[j:best])
                out.append(measured[best])
                j = best + 1
        out.extend(measured[j:])
        out.sort(key=lambda p: p[1])
        return out


    @staticmethod
    def _merge_east(modeled, measured, window):
        """Upstream-modelled and Metro Center-measured eastbound arrivals, as
        one ascending (line, minutes) list.

        Matched line by line, so an Orange model is never mistaken for a Silver
        train that happens to be a minute away from it. Measured wins where both
        have a train; unmatched measured trains are the ones already past
        Clarendon or Pentagon, which is the whole reason for reading C01.
        """
        if not measured:
            return modeled
        out = []
        for line in (LINE_OR, LINE_SV, LINE_BL):
            out += MetroApi._overlay_measured(
                [p for p in modeled if p[0] == line],
                [p for p in measured if p[0] == line], window)
        out.sort(key=lambda p: p[1])
        return out

    @staticmethod
    def _safe_sort_key(t):
        arrival = t['arrival']
        try:
            return int(arrival)
        except (ValueError, TypeError):
            return float('inf')
    
    
    @staticmethod
    def _get_line_color(line: str) -> int:
        if line == LINE_RD:
            color = dim_color(0xFF0000)
        elif line == LINE_OR:
            color = dim_color(0xFF6600, 0.25)  # Brighter orange with more green
        elif line == LINE_YL:
            color = dim_color(0xFFFF00)
        elif line == LINE_GR:
            color = dim_color(0x00FF00)
        elif line == LINE_BL:
            color = dim_color(0x0000FF)
        elif line == LINE_SV:
            color = dim_color(0x999999)
        else:
            color = dim_color(0xAAAAAA)
        
        return color

    

    @staticmethod
    def _apply_transfer_logic(all_trains, mc_preds):
        """Mark each southbound train TAKE or SKIP based on the eastbound it catches.

        The question is about *time*, not identity: skip a train when waiting for
        the next one still puts you on an eastbound no later than this one does.
        Asking it that way survives duplicate entries for a single train — both
        lookups land on the same minute and the skip fires anyway.

        Keyed on direction, not destination: the ride is only as far as Metro
        Center, so a Silver Spring train transfers exactly like a Glenmont one.
        """
        offset = RIDE_DUPONT_TO_MC + WALK_TRANSFER

        south_trains = [t for t in all_trains if t.get('southbound')]
        if not south_trains:
            return

        conns = []
        for t in south_trains:
            eta = int(t['arrival']) + offset
            conn = None
            for p in mc_preds:
                if p[1] >= eta:
                    conn = p
                    break
            conns.append(conn)

        for i, t in enumerate(south_trains):
            conn = conns[i]
            nxt = conns[i + 1] if i + 1 < len(conns) else None

            if conn is None:
                t['skip_mode'] = True
                t['skip_reason'] = "no_data"
            elif nxt is not None and nxt[1] - conn[1] <= SKIP_TOLERANCE:
                t['skip_mode'] = True
                t['skip_reason'] = "efficiency"
            else:
                t['line_color'] = MetroApi._get_line_color(conn[0])

        skips = sum(1 for t in south_trains if t['skip_mode'])
        print(f"🧠 Transfer: {len(south_trains)} southbound, {len(mc_preds)} conn, {skips} skipped")
