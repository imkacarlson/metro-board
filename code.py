# DC Metro Board
import gc
import time
import storage
import supervisor

import bootlog
import rollback

# NOTHING may touch the filesystem before the matrix is built. The RGBMatrix
# framebuffer is a large allocation from outside the Python heap, and flash
# writes draw buffers from that same pool — six log lines were enough to make
# Matrix() fail with "Failed to allocate RGBMatrix buffer" while 92 KB of Python
# heap sat unused. That is why the mount decision, and every log write, now
# happens *after* the display exists rather than before it. Use bootlog.defer()
# until then; it holds lines in RAM and prints to serial.
bootlog.defer('code: importing, free=%d' % gc.mem_free())
from config import config
bootlog.defer('code: config ok, free=%d' % gc.mem_free())
from train_board import TrainBoard
bootlog.defer('code: train_board ok, free=%d' % gc.mem_free())
from metro_api import MetroApi, MetroApiOnFireException
bootlog.defer('code: metro_api ok, free=%d' % gc.mem_free())

STATION_CODE = config['metro_station_code']
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

	if supervisor.runtime.usb_connected:
		# Tethered: boot.py left the filesystem read-only, so ota.py could not
		# install anything even if we handed off to it. Without this check a
		# published update would loop — hand off, decline to write, reset, find
		# the same mismatch, hand off again.
		print('OTA: USB connected — skipping update check (drag-and-drop mode)')
		return

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

	attempts = rollback.ota_attempts(remote)
	if attempts >= rollback.MAX_OTA_ATTEMPTS:
		# Don't even hand off. Every handoff costs a reboot, so a version that
		# will not install would otherwise cycle the board indefinitely.
		bootlog.append('OTA: %s failed %d times already — not retrying'
			% (remote, attempts))
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

	# Bootstrap the safety net. Until some build has proved itself there is
	# nothing to roll back to, so the first healthy run snapshots itself.
	if not rollback.have_backup():
		try:
			rollback.snapshot()
			bootlog.append('code: snapshotted this build to %s' % rollback.BACKUP_DIR)
		except Exception as e:
			bootlog.append('code: could not snapshot: %s' % e)

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
		return MetroApi.fetch_train_predictions(STATION_CODE)
	except MetroApiOnFireException:
		print('WMATA Api is currently on fire. Trying again later ...')
		return None

# The matrix goes up first, before the mount decision and before anything is
# written to flash. This is the single most order-sensitive line in the file.
bootlog.defer('code: constructing display...')
try:
	train_board = TrainBoard(refresh_trains)
except Exception as e:
	bootlog.defer('code: TrainBoard FAILED %s: %s' % (type(e).__name__, e))
	_pending_failure = e
else:
	_pending_failure = None
	bootlog.defer('code: display up, free=%d' % gc.mem_free())

# Now the buffer is claimed, so the filesystem is safe to touch.
#
# usb_connected is finally truthful here — the USB stack is running, unlike in
# boot.py where it has not started. It still needs a moment: code.py begins
# immediately after boot.py and the host may not have finished enumerating, so
# reading it once races and loses. A host resolves in well under a second; a
# dumb power brick never enumerates and just costs this timeout once.
#
# storage.remount() also refuses outright when the drive is host-visible, so the
# decision stays correct even if usb_connected were somehow wrong.
USB_ENUMERATION_TIMEOUT = 3.0

_deadline = time.monotonic() + USB_ENUMERATION_TIMEOUT
while not supervisor.runtime.usb_connected and time.monotonic() < _deadline:
	time.sleep(0.05)

WRITABLE = False
if supervisor.runtime.usb_connected:
	print('mount: USB connected — read-only to Python, drag-and-drop enabled')
else:
	try:
		storage.remount('/', readonly=False)
		WRITABLE = True
		print('mount: standalone — filesystem writable, OTA enabled')
	except RuntimeError as e:
		# Benign: no logging and no updates, but the board still shows trains.
		print('mount: remount refused, OTA disabled:', e)

bootlog.defer('code: mount usb_connected=%s writable=%s'
	% (supervisor.runtime.usb_connected, WRITABLE))
bootlog.flush()

if _pending_failure is not None:
	# Display never came up. The log is on disk now, so fail loudly.
	raise _pending_failure

# Button last: claiming board.BUTTON_UP before the matrix is built risks the
# HUB75 driver finding a pin already in use. This way a conflict costs only the
# UP-button shortcut, which _update_button() swallows, not the whole board.
up_button = _update_button()
bootlog.append('code: button=%s, free=%d' % (up_button is not None, gc.mem_free()))

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
				bootlog.append('code: first refresh ok, free=%d' % gc.mem_free())
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
		# Runtime failures are caught here and never reach recover.py, so this
		# is the only record of them.
		bootlog.append('code: exception in main loop, free=%d: %s'
			% (gc.mem_free(), e))
		print("Rebooting...\n")
		time.sleep(5)
		supervisor.reload()
