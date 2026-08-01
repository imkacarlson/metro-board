"""Runs when code.py exits with an exception. Never OTA-updatable.

boot.py arms this with supervisor.set_next_code_file(reload_on_error=True),
which is the only thing that regains control after an *import-time* failure in
code.py — a syntax error in a freshly downloaded file, say.

A boot counter alone cannot do this job. CircuitPython calls run_boot_py()
exactly once per hard reset, outside the run loop in main.c, so boot.py does not
re-run on supervisor.reload(); a counter kept only there would need three
physical power cycles to notice a broken build. This file runs on the soft
reload that follows the error, so recovery is automatic and immediate.
"""
import microcontroller
import supervisor

import bootlog
import rollback

# CircuitPython preserves the traceback across the soft reload that brought us
# here (a hard reset would clear it), so this is the one place the real cause of
# a shelf-side crash can be captured and read back later over USB.
try:
    tb = supervisor.get_previous_traceback()
except AttributeError:
    tb = None

bootlog.append('recover: code.py exited with an exception')
if tb:
    bootlog.append(tb)
else:
    bootlog.append('recover: no traceback available')

if supervisor.runtime.usb_connected:
    # Tethered to a computer: this is a person debugging. Leave the traceback
    # on screen instead of resetting it away, and do not fight their edits.
    bootlog.append('recover: USB connected — leaving the traceback for you to read')

elif not rollback.have_backup():
    # Nothing to roll back to. Stopping here is deliberate: a reset would just
    # loop on the same failure and wear the flash for nothing.
    bootlog.append('recover: no %s to restore from, stopping' % rollback.BACKUP_DIR)

elif rollback.failed_boots() > rollback.MAX_FAILED_BOOTS:
    # Only a healthy run clears this counter, so passing the cap means rolling
    # back has already been tried and did not help.
    bootlog.append('recover: %d failed boots, rollback is not helping — stopping'
          % rollback.failed_boots())
    bootlog.append('recover: double-press reset for safe mode to get the USB drive back')

else:
    try:
        bootlog.append('recover: restored %d file(s) from %s'
              % (rollback.restore(), rollback.BACKUP_DIR))
        # Hard reset rather than supervisor.reload(), so boot.py runs again and
        # re-arms this hook for the restored build.
        microcontroller.reset()
    except OSError as e:
        bootlog.append('recover: rollback failed: %s' % e)
