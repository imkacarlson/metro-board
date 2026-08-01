# DC Metro Board
import time
import supervisor

import rollback
from config import config
from train_board import TrainBoard
from metro_api import MetroApi, MetroApiOnFireException

STATION_CODE = config['metro_station_code']
TRAIN_GROUP = config['train_group']
REFRESH_INTERVAL = config['refresh_interval']
NO_SERVICE_START_HOUR = config['no_service_start_hour']
NO_SERVICE_END_HOUR = config['no_service_end_hour']

#############################
# Over-the-air updates      #
#############################

# Same pinned owner/repo/branch as ota.py. This file only ever *reads* the
# version; ota.py is the only thing that writes to the filesystem.
MANIFEST_URL = 'https://raw.githubusercontent.com/imkacarlson/metro-board/deploy/manifest.json'
UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # seconds

_last_update_check = 0
_marked_healthy = False


def _remote_version():
	"""Fetch the ~500-byte manifest and pull the version out of it.

	Small enough to do from inside the running app, where free RAM is ~18 KB.
	Downloading the actual files is not — that is ota.py's job.
	"""
	import gc
	import metro_api

	metro_api._init_network()
	gc.collect()

	response = metro_api._network.fetch(MANIFEST_URL)
	try:
		if response.status_code != 200:
			print(f'OTA: manifest returned HTTP {response.status_code}')
			return None
		body = response.text
	finally:
		response.close()
		gc.collect()

	version = metro_api.extract_field(body, '"version":"')
	del body
	gc.collect()
	return version


def check_for_update():
	"""Compare the published version against ours; hand off to ota.py if they differ."""
	global _last_update_check
	_last_update_check = time.monotonic()

	try:
		remote = _remote_version()
	except Exception as e:
		print(f'OTA: version check failed, carrying on: {e}')
		return

	if not remote:
		print('OTA: no version in manifest')
		return

	blocked = rollback.read_line(rollback.BLOCKED_FILE)
	if remote == blocked:
		# This version was rolled back after it failed to boot. Sit tight until
		# a different one is published rather than loop on it.
		print(f'OTA: {remote} is quarantined after a failed boot — skipping')
		return

	local = rollback.read_line(rollback.VERSION_FILE)
	if remote == local:
		print(f'OTA: up to date ({local})')
		return

	print(f'OTA: {local or "unknown"} -> {remote}, handing off to ota.py')
	supervisor.set_next_code_file('ota.py')
	supervisor.reload()


def _mark_healthy():
	"""Zero the boot-health counter. This is the ONLY thing that clears it.

	boot.py increments it and recover.py reads it; if nothing ever zeroed it,
	a board that cannot display trains would roll back forever.
	"""
	global _marked_healthy
	if _marked_healthy:
		return
	try:
		rollback.set_failed_boots(0)
	except Exception as e:
		print(f'Could not reset boot-health counter: {e}')
	_marked_healthy = True
	print('Boot marked healthy.')


def _update_button():
	"""The UP button, for pulling an update immediately instead of waiting a day."""
	try:
		import board
		import digitalio

		button = digitalio.DigitalInOut(board.BUTTON_UP)
		button.switch_to_input(pull=digitalio.Pull.UP)
		return button
	except Exception as e:
		print(f'UP button unavailable: {e}')
		return None


#############################
# Train board               #
#############################

def is_no_service_hours():
	"""Check if current time is during no-service hours (2-5 AM)"""
	import time
	t = time.localtime()
	hour = t[3]

	# No service from 2 AM to 5 AM
	return NO_SERVICE_START_HOUR <= hour < NO_SERVICE_END_HOUR

def refresh_trains() -> [dict]:
	# Skip API calls during no-service hours
	if is_no_service_hours():
		print('🌙 No service hours (2-5 AM) - skipping API call')
		return []

	try:
		return MetroApi.fetch_train_predictions(STATION_CODE, TRAIN_GROUP)
	except MetroApiOnFireException:
		print('WMATA Api is currently on fire. Trying again later ...')
		return None

up_button = _update_button()
train_board = TrainBoard(refresh_trains)

last_refresh = 0
last_blink_update = 0
button_was_down = False
BLINK_UPDATE_INTERVAL = 0.05  # Update blink every 50ms for smooth animation

while True:
	try:
		current_time = time.monotonic()

		# Refresh train data every REFRESH_INTERVAL seconds
		if current_time - last_refresh >= REFRESH_INTERVAL:
			train_board.refresh()
			last_refresh = current_time

			if not _marked_healthy:
				# Order matters: prove this build works and clear the rollback
				# counter *before* an update can send us round again, otherwise
				# a run of successful updates would look like a run of failures.
				_mark_healthy()
				check_for_update()

		# Update blinks at consistent intervals for smooth animation
		if current_time - last_blink_update >= BLINK_UPDATE_INTERVAL:
			train_board.update_blink()
			last_blink_update = current_time

		# On demand via the UP button, otherwise once a day.
		button_down = up_button is not None and not up_button.value
		if button_down and not button_was_down:
			print('UP button pressed — checking for an update')
			check_for_update()
		elif _marked_healthy and current_time - _last_update_check >= UPDATE_CHECK_INTERVAL:
			check_for_update()
		button_was_down = button_down

		time.sleep(0.01)  # Smaller sleep for more responsive timing
	except Exception as e:
		print(f"Caught exception: {e}")
		print("Rebooting...\n")
		time.sleep(5)
		supervisor.reload()
