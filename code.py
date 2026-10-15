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
import metro_api
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
# The daily check is a reboot (see the main loop), so it only happens in the
# 2-5 AM no-service window. It used to fire 24 h after the last check, which
# after the transfer-window deferral meant 08:31 every weekday. The window does
# the scheduling; the gap, just longer than the 3-hour window, only stops the
# boot that the reboot causes from rebooting again the same night.
UPDATE_CHECK_MIN_GAP = 4 * 60 * 60  # seconds

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


def check_for_update(force=False):
	"""Compare the published version against ours; hand off to ota.py if they differ."""
	global _last_update_check
	_last_update_check = time.monotonic()

	if not force and metro_api.is_transfer_intelligence_time():
		# _remote_version() does a full HTTP manifest fetch, and this runs once
		# per boot right after the first refresh — the tightest-memory moment of
		# the cycle, during the only window that fetches four stations at once.
		# /version.txt already matches on nearly every boot, so it fragments for
		# nothing. The main loop picks it up in the next no-service window.
		print('OTA: transfer window — deferring the update check')
		return

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
	# Before the no-service check, not after: 2-5 AM is where the daily sync is
	# meant to happen, and refresh is the only thing that runs then.
	metro_api.sync_time_if_due()

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

# The update check happens HERE, before the first train fetch, and this position
# is the whole point of it.
#
# One HTTPS fetch permanently claims ~54 KB: free goes 64 K -> 10 K across the
# first refresh and never comes back, because the TLS session and socket buffers
# stay allocated for the life of the run. A manifest fetch to a second host in
# that regime costs another 1-3 KB, which drops the *next* train fetch under
# optimized_fetch's 10000-byte floor. That floor reboots the board, which boots,
# checks for updates again, and loops — 23 cycles of exactly that are in
# /board.log.1. Up here there is 60+ KB free and the same fetch costs nothing
# that matters.
#
# It runs before _mark_healthy(), so a handoff to ota.py leaves the boot counter
# uncleared for one cycle. That is bounded: an install is one handoff plus one
# reset, and the new build clears the counter on its own first refresh, well
# short of rollback.MAX_FAILED_BOOTS.
#
# Guarded because it now runs outside the main loop's except: a failed update
# check must never be the reason the board does not show trains.
try:
	check_for_update()
except Exception as e:
	bootlog.append('code: update check failed, carrying on: %s' % e)

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
				bootlog.append('code: first refresh ok, free=%d' % gc.mem_free())
				_mark_healthy()

		# Update blinks at consistent intervals for smooth animation
		if current_time - last_blink_update >= BLINK_UPDATE_INTERVAL:
			train_board.update_blink()
			last_blink_update = current_time

		# On demand via the UP button, otherwise once a night. The nightly path
		# reboots instead of checking in place: by now the first fetch has taken
		# its ~54 KB and a manifest fetch here is what started the reboot loop.
		# A reboot lands in the high-memory regime where the check belongs, and
		# only in the no-service window, when nobody is reading the board. The
		# boot it causes also gets the nightly clock sync.
		button_down = up_button is not None and not up_button.value
		if button_down and not button_was_down:
			print('UP button pressed — checking for an update')
			check_for_update(force=True)
		elif (_marked_healthy
				and current_time - _last_update_check >= UPDATE_CHECK_MIN_GAP
				and metro_api.is_quiet_hours()):
			bootlog.append('code: nightly update check — rebooting into it')
			supervisor.reload()
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
