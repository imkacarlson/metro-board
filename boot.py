"""Anchor file — deliberately NOT over-the-air updatable.

Because nothing remote can ever rewrite this file, it is always the one piece of
code guaranteed to run, which makes it the right place to keep the recovery
path. It has two jobs:

  1. Decide whether the filesystem is writable. Tethered to a computer the board
     stays read-only so Windows drag-and-drop keeps working; on a dumb power
     brick it remounts writable so ota.py can install updates.

  2. Roll back. A counter in non-volatile memory is bumped on every boot and
     zeroed by code.py once the display has actually refreshed. If it reaches
     MAX_FAILED_BOOTS, the last known-good files are restored from /backup and
     the version that broke us is written to /ota_blocked.txt so code.py will
     not immediately download it again.
"""
import os
import storage
import supervisor
import microcontroller

OTA_FILES = ('code.py', 'config.py', 'metro_api.py', 'train_board.py')
BACKUP_DIR = '/backup'
VERSION_FILE = '/version.txt'
BLOCKED_FILE = '/ota_blocked.txt'

NVM_HEALTH_SLOT = 0
MAX_FAILED_BOOTS = 3
COPY_CHUNK = 512


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _copy(src, dst):
    with open(src, 'rb') as fsrc:
        with open(dst, 'wb') as fdst:
            while True:
                chunk = fsrc.read(COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)


def _read_line(path):
    try:
        with open(path, 'r') as f:
            return f.read(64).strip()
    except OSError:
        return ''


def _roll_back():
    """Restore /backup over the live files. Returns number of files restored."""
    # Quarantine the version that failed *before* touching anything, so the
    # board does not just re-download the same broken build on the next boot.
    failed_version = _read_line(VERSION_FILE)
    if failed_version:
        try:
            with open(BLOCKED_FILE, 'w') as f:
                f.write(failed_version)
            print('boot: quarantined version', failed_version)
        except OSError as e:
            print('boot: could not write quarantine file:', e)

    restored = 0
    for name in OTA_FILES:
        src = BACKUP_DIR + '/' + name
        if _exists(src):
            _copy(src, '/' + name)
            restored += 1

    backup_version = BACKUP_DIR + VERSION_FILE
    if _exists(backup_version):
        _copy(backup_version, VERSION_FILE)
    elif _exists(VERSION_FILE):
        os.remove(VERSION_FILE)

    return restored


writable = False
if supervisor.runtime.usb_connected:
    print('boot: USB connected — filesystem read-only, drag-and-drop enabled')
else:
    try:
        storage.remount('/', readonly=False)
        writable = True
        print('boot: standalone — filesystem writable, OTA enabled')
    except RuntimeError as e:
        # Benign: the board simply never updates and keeps running what it has.
        print('boot: remount failed, OTA disabled:', e)

failed_boots = microcontroller.nvm[NVM_HEALTH_SLOT]
if failed_boots == 255:
    # Erased flash reads as 0xFF. Treat that as "never initialised", not as
    # 255 consecutive failures.
    failed_boots = 0

if failed_boots >= MAX_FAILED_BOOTS:
    print('boot: %d consecutive boots never reached a healthy state' % failed_boots)
    if writable and _exists(BACKUP_DIR):
        try:
            print('boot: rolled back %d file(s) from %s' % (_roll_back(), BACKUP_DIR))
            failed_boots = 0
        except OSError as e:
            print('boot: rollback failed:', e)
    else:
        print('boot: no rollback possible (writable=%s, backup=%s)'
              % (writable, _exists(BACKUP_DIR)))

if failed_boots < 254:
    microcontroller.nvm[NVM_HEALTH_SLOT:NVM_HEALTH_SLOT + 1] = bytes([failed_boots + 1])
