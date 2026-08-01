"""Tiny append-only log, so failures on the shelf can be read back later.

The board behaves differently standalone than it does tethered: the filesystem
is writable, recover.py is armed, and the update check runs. A failure in that
state may not reproduce over USB at all, which is why writing it down on the
board matters.

Never OTA-updatable, and every write is swallowed on error — logging must not
be capable of becoming the thing that breaks the board. When the filesystem is
read-only (tethered) append() silently does nothing but still prints to serial.
"""
LOG_FILE = '/board.log'
MAX_BYTES = 12000


def append(msg):
    print(msg)
    try:
        import os
        try:
            if os.stat(LOG_FILE)[6] > MAX_BYTES:
                # Simple wrap rather than rotation. Keeps flash wear bounded and
                # the newest boot is always the interesting one.
                os.remove(LOG_FILE)
        except OSError:
            pass
        with open(LOG_FILE, 'a') as f:
            f.write(msg)
            f.write('\n')
    except Exception:
        # Read-only filesystem, full flash, anything: stay quiet.
        pass
