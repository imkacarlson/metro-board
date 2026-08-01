"""Tiny append-only log, so failures on the shelf can be read back later.

The board behaves differently standalone than it does tethered: the filesystem
is writable, and the update check runs. A failure in that state may not
reproduce over USB at all, which is why writing it down on the board matters.

Never OTA-updatable, and every write is swallowed on error — logging must not
be capable of becoming the thing that breaks the board. When the filesystem is
read-only (tethered) writes silently do nothing but still print to serial.

Two entry points, and the difference matters more than it looks:

    defer(msg)  — hold in RAM, print to serial only
    append(msg) — write to flash now

Writing to flash allocates buffers from the same pool the RGBMatrix framebuffer
needs, and that pool is small enough that six log lines were enough to make
Matrix() fail with "Failed to allocate RGBMatrix buffer" while 92 KB of Python
heap sat unused. So nothing may touch flash until the display exists. Use
defer() before the matrix is built, then flush() once it is.
"""
LOG_FILE = '/board.log'
MAX_BYTES = 12000

_pending = []


def _write(msg):
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


def defer(msg):
    """Record a line without touching flash. Safe before the display exists."""
    print(msg)
    _pending.append(msg)


def flush():
    """Write everything defer() has been holding. Call after the matrix is up."""
    global _pending
    for m in _pending:
        _write(m)
    _pending = []


def append(msg):
    print(msg)
    _write(msg)
