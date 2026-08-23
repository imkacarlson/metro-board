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
OLD_LOG_FILE = '/board.log.1'
MAX_BYTES = 12000

_pending = []


def _stamp(msg):
    """Timestamp a line at the moment it is recorded.

    Stamped here rather than in _write() on purpose: a deferred line is written
    at flush() time, so stamping there would give every boot line the same
    flush timestamp and lose the one thing defer() exists to record — when each
    import actually completed.

    A leading ~ marks a clock that has not been set from the network yet, so a
    boot-time line is never mistaken for a real wall-clock one.
    """
    try:
        import time
        t = time.localtime()
        if t[0] < 2024:
            return '~%02d:%02d:%02d %s' % (t[3], t[4], t[5], msg)
        return '%04d-%02d-%02d %02d:%02d:%02d %s' % (t[0], t[1], t[2],
                                                     t[3], t[4], t[5], msg)
    except Exception:
        return msg


def _write(msg):
    try:
        import os
        try:
            if os.stat(LOG_FILE)[6] > MAX_BYTES:
                # Rotate, one generation. The old wrap simply removed the file,
                # which destroyed evidence: the log recovered from the morning
                # reboot loop began mid-boot-cycle with the earlier cycles gone.
                # Worst case is 24 KB against 1532 KB free.
                try:
                    os.remove(OLD_LOG_FILE)
                except OSError:
                    pass
                os.rename(LOG_FILE, OLD_LOG_FILE)
        except OSError:
            # Unchanged failure mode: if the rotation cannot happen the append
            # still does, silently.
            pass
        with open(LOG_FILE, 'a') as f:
            f.write(msg)
            f.write('\n')
    except Exception:
        # Read-only filesystem, full flash, anything: stay quiet.
        pass


def defer(msg):
    """Record a line without touching flash. Safe before the display exists."""
    msg = _stamp(msg)
    print(msg)
    _pending.append(msg)


def flush():
    """Write everything defer() has been holding. Call after the matrix is up."""
    global _pending
    for m in _pending:
        _write(m)
    _pending = []


def append(msg):
    msg = _stamp(msg)
    print(msg)
    _write(msg)
