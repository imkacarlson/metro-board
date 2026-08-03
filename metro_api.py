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
WALK_TRANSFER = config['walk_transfer']
RIDE_DUPONT_TO_MC = config['ride_dupont_to_mc']
SKIP_TOLERANCE = config['skip_tolerance']
DUPONT_MATCH_WINDOW = config['dupont_match_window']
LOADING_MIN_TEXT = config['loading_min_text']
NUM_TRAINS = config['num_trains']
TRANSFER_START_HOUR = config['transfer_start_hour']
TRANSFER_START_MINUTE = config['transfer_start_minute']
TRANSFER_END_HOUR = config['transfer_end_hour']
TRANSFER_END_MINUTE = config['transfer_end_minute']

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

def optimized_fetch(api_path):
    """Ultra-memory-efficient fetch with direct chunk parsing - no string assembly"""
    global _network
    import gc
    
    _init_network()
    
    # PROVEN TECHNIQUE: Multiple gc.collect() calls before large allocation
    # Each call frees more "phantom" objects from previous network operations
    for i in range(4):
        gc.collect()
    
    # Check memory after cleanup
    free_before = gc.mem_free()
    if free_before < 10000:  # Increase threshold 
        print(f"⚠️ Memory fragmentation detected ({free_before} bytes free)")
        print("🔄 Performing emergency memory reset...")
        # NUCLEAR OPTION: supervisor.reload() to completely reset memory
        import supervisor
        supervisor.reload()
    
    try:
        log_memory("After defrag cleanup")
        
        # Build full URL
        api_url = f"{METRO_API_URL}{api_path}"
        
        # Make request
        response = _network.fetch(api_url, headers={'api_key': METRO_API_KEY})
        log_memory("After fetch")
        
        # REVOLUTIONARY APPROACH: Parse directly from chunks without building full string
        trains = direct_chunk_parse_trains(response)
        
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
_rd_glen_preds = []   # arrival mins @Dupont for Glenmont trains, modelled from Tenleytown
_rd_south_measured = []  # arrival mins @Dupont for Glenmont trains, off Dupont's own board
_rd_north_preds = []  # (destination, arrival mins) @Dupont for northbound trains

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
    current_time = f"{hour:02d}:{minute:02d}"
    print(f"⏰ Current time check: {current_time}")
    
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
DEST_GLENMONT = 'Glenmont'
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

def normalize_destination(dest):
    """Shorten known long destination names so they survive display truncation."""
    if dest in (DEST_SHADY_GROVE, DEST_SHADY_GRV):
        return DEST_SHADY_GRO
    return dest

def log_memory(label):
    """Enhanced memory monitoring with stack tracking"""
    import gc
    import supervisor
    global _max_mem_seen, _min_mem_seen, _request_count
    
    gc.collect()
    free = gc.mem_free()
    allocated = gc.mem_alloc()
    
    # Track min/max
    _max_mem_seen = max(_max_mem_seen, free)
    _min_mem_seen = min(_min_mem_seen, free)
    
    # Log detailed stats every 20 cycles
    if _request_count % 20 == 0:
        try:
            stack_size = supervisor.runtime.pystack_size if hasattr(supervisor.runtime, 'pystack_size') else 'N/A'
            print(f"STATS: free={free}, alloc={allocated}, max_seen={_max_mem_seen}, min_seen={_min_mem_seen}, stack={stack_size}")
        except:
            print(f"STATS: free={free}, alloc={allocated}, max_seen={_max_mem_seen}, min_seen={_min_mem_seen}")
    else:
        print(f"{label}: {free} bytes free")
    
    return free

def direct_chunk_parse_trains(response):
    """Revolutionary zero-string-assembly parser that processes HTTP chunks directly"""
    import gc
    trains = []
    
    # State machine for incremental JSON parsing
    buffer = ""  # Small rolling buffer for incomplete JSON fragments
    train_count = 0
    in_trains_array = False
    brace_depth = 0
    current_train = ""
    
    try:
        for chunk in response.iter_content(256):  # Smaller chunks for better memory control
            if not chunk:
                continue
                
            # Decode chunk and add to rolling buffer  
            if isinstance(chunk, bytes):
                try:
                    chunk_text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    chunk_text = chunk.decode('utf-8', 'replace')
            else:
                chunk_text = str(chunk)
            buffer += chunk_text
            
            # Process complete train objects from buffer
            while True:
                if not in_trains_array:
                    # Look for start of Trains array
                    trains_pos = buffer.find('"Trains":[')
                    if trains_pos != -1:
                        in_trains_array = True
                        buffer = buffer[trains_pos + 10:]  # Skip past "Trains":[
                        continue
                    else:
                        # Keep only last 50 chars to catch split patterns
                        if len(buffer) > 50:
                            buffer = buffer[-50:]
                        break
                
                # We're in trains array, look for complete train objects
                if not current_train:
                    # Look for start of train object
                    brace_start = buffer.find('{')
                    if brace_start == -1:
                        # Keep buffer small while waiting for next object
                        if len(buffer) > 20:
                            buffer = buffer[-20:]
                        break
                    buffer = buffer[brace_start:]
                    current_train = ""
                    brace_depth = 0
                
                # Build current train object character by character
                i = 0
                while i < len(buffer):
                    char = buffer[i]
                    current_train += char
                    
                    if char == '{':
                        brace_depth += 1
                    elif char == '}':
                        brace_depth -= 1
                        if brace_depth == 0:
                            # Complete train object found - parse it immediately
                            train_data = parse_train_fragment(current_train)
                            if train_data:
                                trains.append(train_data)
                                train_count += 1
                                if train_count >= 50:  # Prevent runaway parsing
                                    return trains
                            
                            # Reset for next train
                            current_train = ""
                            buffer = buffer[i+1:]  # Remove processed portion
                            break
                    
                    i += 1
                else:
                    # Incomplete train object, buffer is consumed
                    buffer = ""
                    break
            
            # Memory management - force garbage collection every few chunks
            if train_count % 10 == 0:
                gc.collect()
            
            # Safety: prevent buffer from growing too large
            if len(buffer) > 1000:
                print(f"⚠️ Buffer overflow protection: {len(buffer)} chars")
                buffer = buffer[-500:]  # Keep only recent data
        
    except Exception as e:
        print(f"Direct chunk parsing error: {e}")
        gc.collect()
    
    return trains

def parse_train_fragment(train_json):
    """Parse a single complete train JSON object - ultra-lightweight"""
    try:
        line = extract_field(train_json, '"Line":"')
        dest = extract_field(train_json, '"Destination":"')
        mins = extract_field(train_json, '"Min":"')
        group = extract_field(train_json, '"Group":"')
        location = extract_field(train_json, '"LocationCode":"')
        
        if line and dest and location:
            return {
                'Line': line,
                'Destination': dest, 
                'Min': mins or '0',
                'Group': group or '1',
                'LocationCode': location
            }
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

def fetch_simple_dupont_trains(station_code: str) -> [dict]:
    """Memory-efficient single-station fetch for non-transfer hours"""
    import gc
    import time
    global _last_sync_time, _sync_interval
    
    # Sync with time server every ~3 hours
    current_time = time.time()
    if current_time - _last_sync_time > _sync_interval:
        update_system_time()
    
    print("🚇 Simple mode: Fetching Dupont Circle only")
    # No ?TrainGroup= — WMATA ignores it. Simple mode deliberately shows both
    # directions merged, which is the long-standing off-peak behaviour.
    api_path = station_code
    
    try:
        all_trains = optimized_fetch(api_path)
        if not all_trains:
            print("❌ No train data received from simple API call")
            return []
        
        # Filter to trains 7+ minutes away and take top N
        filtered_trains = []
        for t in all_trains:
            dest = t.get('Destination', '')
            if dest in NON_PASSENGER:
                continue

            mins = MetroApi._parse_min(t.get('Min'))
            if mins is not None and mins >= 7:
                line_color = MetroApi._get_line_color(t.get('Line', ''))
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
        print(f"✅ Simple mode: {len(filtered_trains)} trains 7+ minutes away")
        return filtered_trains
        
    except Exception as e:
        print(f"Simple fetch failed: {e}")
        gc.collect()
        return []

class MetroApi:
    @staticmethod
    def fetch_train_predictions(station_code: str) -> [dict]:
        import gc
        import time
        global _network, _request_count, _osv_preds, _bl_preds, _rd_glen_preds, _rd_south_measured, _rd_north_preds
        global _last_sync_time, _sync_interval
        
        # Sync with time server every ~3 hours
        current_time = time.time()
        if current_time - _last_sync_time > _sync_interval:
            update_system_time()
        
        # Check if we should use transfer intelligence or simple mode
        if not is_transfer_intelligence_time():
            print("⏰ Outside transfer hours - using simple mode")
            return fetch_simple_dupont_trains(station_code)
        
        print("⏰ Transfer intelligence time - using full complexity mode")
        _init_network()
        gc.collect()
        log_memory("Start of fetch_train_predictions")

        # Station codes from config
        main_station = station_code
        osv_station, osv_offset = OSV_PRED_SOURCE
        bl_station, bl_offset = BL_PRED_SOURCE
        rd_glen_station, rd_glen_offset = RD_GLEN_PRED_SOURCE

        # Four stations, same as before: two eastbound-connection sources, the
        # Glenmont upstream source, and Dupont itself for northbound.
        # No ?TrainGroup= — WMATA silently ignores it (verified: identical
        # responses with and without), so direction is decided here instead.
        all_stations = f"{osv_station},{bl_station},{rd_glen_station},{main_station}"
        api_path = all_stations

        for i in range(METRO_API_RETRIES + 1):
            gc.collect()
            log_memory("Before network check")
            _request_count += 1

            try:
                print(f"🚇 Fetching combined data for stations: {all_stations}")
                log_memory("Before combined fetch")
                
                all_trains = optimized_fetch(api_path)
                if not all_trains:
                    print("❌ No train data received from combined API call")
                    return MetroApi._fallback_display()

                # Clear all prediction caches
                _osv_preds.clear()
                _bl_preds.clear()
                _rd_glen_preds.clear()
                _rd_south_measured.clear()
                _rd_north_preds.clear()

                # Process all trains from the single response
                main_display_trains = []
                for t in all_trains:
                    loc = t['LocationCode']
                    
                    # Upstream OR/SV predictions (Clarendon)
                    if loc == osv_station and t['Line'] in (LINE_OR, LINE_SV) and t['Destination'] in (DEST_NEW_CARROLLTON, DEST_N_CARROLLTON, DEST_NEWCRLTON, DEST_LARGO):
                        mins = MetroApi._parse_min(t['Min'])
                        if mins is None:
                            continue
                        _osv_preds.append((str(t['Line']), mins + osv_offset))

                    # Upstream BL predictions (Pentagon)
                    elif loc == bl_station and t['Line'] == LINE_BL and t['Destination'] in (DEST_NEW_CARROLLTON, DEST_N_CARROLLTON, DEST_NEWCRLTON, DEST_LARGO):
                        mins = MetroApi._parse_min(t['Min'])
                        if mins is None:
                            continue
                        _bl_preds.append((str(t['Line']), mins + bl_offset))

                    # Upstream RD-Glenmont predictions (Tenleytown)
                    elif loc == rd_glen_station and t['Line'] == LINE_RD and t['Destination'] == DEST_GLENMONT:
                        mins = MetroApi._parse_min(t['Min'])
                        if mins is not None and mins + rd_glen_offset >= 7:
                            _rd_glen_preds.append(mins + rd_glen_offset)

                    # Dupont's own southbound board. Same trains as the Tenleytown
                    # model once they are close enough for Dupont to see them, but
                    # measured rather than modelled — see _overlay_measured.
                    elif loc == main_station and t['Line'] == LINE_RD and t['Group'] == SOUTHBOUND_GROUP:
                        if t['Destination'] != DEST_GLENMONT:
                            continue
                        mins = MetroApi._parse_min(t['Min'])
                        if mins is not None:
                            _rd_south_measured.append(mins)

                    # Northbound, straight off Dupont's own board. No destination
                    # filter and no offset — these trains stop here. Group is the
                    # direction; anything you can actually board counts.
                    elif loc == main_station and t['Line'] == LINE_RD and t['Group'] == NORTHBOUND_GROUP:
                        if t['Destination'] in NON_PASSENGER:
                            continue
                        mins = MetroApi._parse_min(t['Min'])
                        if mins is not None and mins >= 7:
                            _rd_north_preds.append((normalize_destination(t['Destination']), mins))

                del all_trains
                gc.collect()

                # Sort Red Line caches by arrival time (earliest first)
                _rd_glen_preds.sort()
                _rd_south_measured.sort()
                _rd_north_preds.sort(key=lambda p: p[1])

                print(f"✅ Cached: {len(_osv_preds)} OR/SV, {len(_bl_preds)} BL, {len(_rd_glen_preds)} RD-Glen, {len(_rd_south_measured)} RD-South, {len(_rd_north_preds)} RD-North")

                glen_mins = MetroApi._overlay_measured(_rd_glen_preds, _rd_south_measured, DUPONT_MATCH_WINDOW)

                # Rehydrate predictions into the main display list
                for m in glen_mins:
                    main_display_trains.append({'line_color': MetroApi._get_line_color(LINE_RD), 'destination': DEST_GLENMONT, 'arrival': str(m), 'skip_mode': False, 'skip_reason': None})
                for dest, m in _rd_north_preds:
                    main_display_trains.append({'line_color': MetroApi._get_line_color(LINE_RD), 'destination': dest, 'arrival': str(m), 'skip_mode': False, 'skip_reason': None})
                
                # Sort all trains by arrival time
                main_display_trains.sort(key=MetroApi._safe_sort_key)

                # Filter to trains 7+ minutes away — the whole list, not just the
                # top N. The third row's SKIP decision needs a successor to
                # compare against, so the cut to NUM_TRAINS happens afterwards.
                filtered_trains = []
                for t in main_display_trains:
                    if isinstance(t['arrival'], str) and t['arrival'].isdigit() and int(t['arrival']) >= 7:
                        filtered_trains.append(t)

                all_east_preds = _osv_preds + _bl_preds
                all_east_preds.sort(key=lambda p: p[1]) # Sort by arrival time
                MetroApi._apply_transfer_logic(filtered_trains, all_east_preds)

                display_trains = filtered_trains[:NUM_TRAINS]
                del main_display_trains, filtered_trains
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
            if t['destination'] == DEST_GLENMONT:
                t['line_color'] = MetroApi._get_line_color(LINE_RD)
        return prior

    @staticmethod
    def _overlay_measured(modeled, measured, window):
        """Prefer Dupont's own figure where the same train appears in both sorted
        lists. Trains >7 min out are still behind Tenleytown, so they show in both."""
        if not measured:
            return modeled

        out = []
        j = 0
        for m in modeled:
            while j < len(measured) and measured[j] < m - window:
                j += 1
            if j < len(measured) and measured[j] <= m + window:
                out.append(measured[j])
                j += 1
            else:
                out.append(m)
        out.sort()
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
        """Mark each Glenmont train TAKE or SKIP based on the eastbound it catches.

        The question is about *time*, not identity: skip a train when waiting for
        the next one still puts you on an eastbound no later than this one does.
        Asking it that way survives duplicate entries for a single train — both
        lookups land on the same minute and the skip fires anyway.
        """
        offset = RIDE_DUPONT_TO_MC + WALK_TRANSFER

        glen_trains = [t for t in all_trains if t['destination'] == DEST_GLENMONT]
        if not glen_trains:
            return

        print(f"🧠 Applying transfer logic to {len(glen_trains)} Glenmont train(s) with {len(mc_preds)} connection(s).")
        conns = []

        for i, t in enumerate(glen_trains):
            eta = int(t['arrival']) + offset
            print(f"  - Glenmont train #{i+1} (arrives Dupont in {t['arrival']} min):")
            print(f"    - Calculated ETA at Metro Center: {eta} min")

            conn = None
            for p in mc_preds:
                if p[1] >= eta:
                    conn = p
                    print(f"    - Found potential connection: {p[0]} line in {p[1]} min")
                    break

            if conn is None:
                print("    - No suitable connection found.")
            conns.append(conn)

        for i, t in enumerate(glen_trains):
            conn = conns[i]
            nxt = conns[i + 1] if i + 1 < len(conns) else None

            print(f"  - Decision for Glenmont train #{i+1}:")
            if conn is None:
                t['skip_mode'] = True
                t['skip_reason'] = "no_data"
                print("    - SKIP ⏭️ (No connection available) [reason: no_data]")
            elif nxt is not None and nxt[1] - conn[1] <= SKIP_TOLERANCE:
                t['skip_mode'] = True
                t['skip_reason'] = "efficiency"
                print(f"    - SKIP ⏭️ (Next Glenmont train reaches an eastbound at {nxt[1]} min vs {conn[1]}) [reason: efficiency]")
            else:
                t['line_color'] = MetroApi._get_line_color(conn[0])
                print(f"    - TAKE ✅ (Connects to {conn[0]} line train in {conn[1]} min)")
