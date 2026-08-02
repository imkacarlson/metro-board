"""Shared recovery helpers for boot.py and recover.py.

Importing this module has no side effects. Like boot.py, ota.py and recover.py
it is never over-the-air updatable, so it is always intact when needed.

The boot-health counter lives in non-volatile memory and has exactly one rule:
**only a healthy run zeroes it.** boot.py increments it on every hard reset,
recover.py reads it to decide whether to keep trying, and code.py zeroes it once
the display has actually refreshed. That invariant is what stops a permanently
broken build from thrashing the flash in a rollback loop.
"""
import os
import microcontroller

OTA_FILES = ('code.py', 'config.py', 'metro_api.py', 'train_board.py')
BACKUP_DIR = '/backup'
VERSION_FILE = '/version.txt'
BLOCKED_FILE = '/ota_blocked.txt'

NVM_HEALTH_SLOT = 0
MAX_FAILED_BOOTS = 3
COPY_CHUNK = 512

# A version that keeps failing to install must stop being retried. ota.py hard
# resets back into code.py when an update fails, and code.py checks for updates
# on every boot — so without a cap, one bad publish means a reboot loop every
# ~15 seconds, rewriting the backup each time.
OTA_STATE_FILE = '/ota_state.txt'
MAX_OTA_ATTEMPTS = 3


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def copy(src, dst):
    with open(src, 'rb') as fsrc:
        with open(dst, 'wb') as fdst:
            while True:
                chunk = fsrc.read(COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)


def read_line(path):
    try:
        with open(path, 'r') as f:
            return f.read(64).strip()
    except OSError:
        return ''


def have_backup():
    return exists(BACKUP_DIR)


def failed_boots():
    n = microcontroller.nvm[NVM_HEALTH_SLOT]
    # Erased flash reads as 0xFF. Treat that as "never initialised" rather than
    # as 255 consecutive failures.
    return 0 if n == 255 else n


def set_failed_boots(n):
    if n > 254:
        n = 254
    microcontroller.nvm[NVM_HEALTH_SLOT:NVM_HEALTH_SLOT + 1] = bytes([n])


def ota_attempts(version):
    """How many times installing this exact version has already been tried."""
    parts = read_line(OTA_STATE_FILE).split(' ')
    if len(parts) == 2 and parts[0] == version:
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


def record_ota_attempt(version):
    """Count an attempt *before* it runs, so a crash mid-install still counts."""
    n = ota_attempts(version) + 1
    try:
        with open(OTA_STATE_FILE, 'w') as f:
            f.write('%s %d' % (version, n))
    except OSError as e:
        print('rollback: could not record OTA attempt:', e)
    return n


def clear_ota_state():
    try:
        os.remove(OTA_STATE_FILE)
    except OSError:
        pass


def snapshot():
    """Record the current files as the known-good build to fall back to.

    Called only from a run that has actually displayed trains, so what gets
    captured is by definition a build that works. Without this the very first
    standalone boot has no safety net: recover.py would find no /backup and
    halt, which is a dead board rather than a rolled-back one.
    """
    if not exists(BACKUP_DIR):
        os.mkdir(BACKUP_DIR)
    for name in OTA_FILES:
        if exists('/' + name):
            copy('/' + name, BACKUP_DIR + '/' + name)
    if exists(VERSION_FILE):
        copy(VERSION_FILE, BACKUP_DIR + VERSION_FILE)


def restore():
    """Put /backup back over the live files. Returns the number restored.

    The version that failed is quarantined first, so code.py will not simply
    re-download the same broken build and loop.
    """
    failed_version = read_line(VERSION_FILE)
    if failed_version:
        try:
            with open(BLOCKED_FILE, 'w') as f:
                f.write(failed_version)
            print('rollback: quarantined version', failed_version)
        except OSError as e:
            print('rollback: could not write quarantine file:', e)

    restored = 0
    for name in OTA_FILES:
        src = BACKUP_DIR + '/' + name
        if exists(src):
            copy(src, '/' + name)
            restored += 1

    backup_version = BACKUP_DIR + VERSION_FILE
    if exists(backup_version):
        copy(backup_version, VERSION_FILE)
    elif exists(VERSION_FILE):
        os.remove(VERSION_FILE)

    return restored
