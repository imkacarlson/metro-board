import os

from adafruit_bitmap_font import bitmap_font

def dim_color(color, brightness=0.15):
    """Dim RGB color by scaling each component"""
    r = int(((color >> 16) & 0xFF) * brightness)
    g = int(((color >> 8) & 0xFF) * brightness)
    b = int((color & 0xFF) * brightness)
    return (r << 16) | (g << 8) | b

# The WMATA API key lives in settings.toml (gitignored), NOT here.
# This file is fetched over the air from a *public* URL, so anything in it
# is public by construction. See settings.toml.template.
_METRO_API_KEY = os.getenv('WMATA_API_KEY')

config = {

	#########################
	# Metro Configuration   #
	#########################

	# Metro Station Code
	'metro_station_code': 'A03',

	# API Key for WMATA — sourced from settings.toml, never committed
	'metro_api_key': _METRO_API_KEY,

	#########################
	# Other Values You      #
	# Probably Shouldn't    #
	# Touch                 #
	#########################
	'metro_api_url': 'https://api.wmata.com/StationPrediction.svc/json/GetPrediction/',
	'metro_api_retries': 2,
	'refresh_interval': 5, # 5 seconds is a good middle ground for updates, as the processor takes its sweet ol time

	# Display Settings
	'matrix_width': 64,
	'num_trains': 3,
	'font': bitmap_font.load_font('lib/5x7.bdf'),
	'character_width': 5,
	'character_height': 7,
	'text_padding': 1,
	'text_color': dim_color(0xFF7500),

	'loading_destination_text': 'Loading',
	'loading_min_text': '---',
	'loading_line_color': dim_color(0xFF00FF), # Something something Purple Line joke

	'heading_text': 'LN DEST   MIN',
	'heading_color': dim_color(0xFF0000),

	'train_line_height': 6,
	'train_line_width': 2,

	'min_label_characters': 3,
	'destination_max_characters': 8,

	# Upstream‑prediction sources for the eastbound connection
	# Each entry: (station_code, minutes_from_this_station_to_MetroCenter)
	'osv_pred_source' : ('K02', 12),   # Clarendon → MC ≈ 12 min
	'bl_pred_source'  : ('C07', 13),   # Pentagon  → MC ≈ 13 min

	# Upstream predictor for southbound Red‑line trains.
	# Northbound is read straight off Dupont's own predictions (Group 2) —
	# see metro_api.NORTHBOUND_GROUP — so it needs no upstream source and
	# survives service-pattern changes such as short-turns at Friendship Heights.
	# 8 is MetroHero's segment sum A07→A06→A05→A04→A03 (2+2+2+2), dwell
	# included; the old 7 was a guess and ran a minute early. metro_api logs an
	# OFFSET_OBS line per train so this can be checked against reality.
	'rd_glen_pred_source' : ('A07', 8),   # Tenleytown → Dupont ≈ +8 min

	# Never show a train you cannot physically reach: door → Dupont platform.
	'display_floor_min'   : 7,

	# Keith's morning‑commute parameters
	'ride_dupont_to_mc'   : 3,   # min on the Red line
	'walk_transfer'       : 1,   # min Red platform → lower

	# Skip a Glenmont train when the next one still reaches an eastbound within
	# this many minutes of the one this train catches. 0 means "only if it is
	# literally the same departure"; a couple of minutes of slack absorbs the
	# noise in upstream predictions.
	'skip_tolerance'      : 2,

	# How far apart (min) a modelled Tenleytown arrival and a measured Dupont
	# arrival can be and still be believed to be the same train.
	'dupont_match_window' : 3,

	# Transfer intelligence time window (weekday mornings only)
	'transfer_start_hour'   : 6,   # Transfer intelligence starts at 6:45 AM
	'transfer_start_minute' : 45,
	'transfer_end_hour'     : 8,   # Transfer intelligence ends at 8:30 AM
	'transfer_end_minute'   : 30,

	# No-service hours (when trains don't run)
	'no_service_start_hour' : 2,   # No trains from 2:00 AM
	'no_service_end_hour'   : 5,   # No trains until 5:00 AM
}
