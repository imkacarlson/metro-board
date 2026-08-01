"""Anchor file — deliberately NOT over-the-air updatable.

Because nothing remote can ever rewrite this file, it is the one piece of code
guaranteed to run, which makes it the right place to own the recovery path.

CircuitPython runs boot.py exactly once per hard reset: main.c calls
run_boot_py() outside the run loop, so supervisor.reload() does not come back
through here. Two things follow, and the whole update design rests on them:

  * The writable remount below survives the soft reload into ota.py, which is
    what lets ota.py write files at all.
  * A boot counter here cannot notice a build that fails on import, because
    nothing here runs again. That is what recover.py is for, armed below.
"""
import storage
import supervisor

import rollback

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

if writable:
    # If code.py exits with an exception, reload into recover.py instead of
    # stopping. Only armed when we can actually write, so a syntax error while
    # tethered still shows you a traceback rather than resetting in a loop.
    supervisor.set_next_code_file(
        'recover.py', reload_on_error=True, sticky_on_error=True)
    print('boot: recovery hook armed')

# Backstop for failures that never raise — a hang, or a crash that resets the
# board outright. recover.py handles anything that throws.
failed = rollback.failed_boots()
if failed >= rollback.MAX_FAILED_BOOTS:
    print('boot: %d boots never reached a healthy state' % failed)
    if writable and rollback.have_backup():
        try:
            print('boot: restored %d file(s) from %s'
                  % (rollback.restore(), rollback.BACKUP_DIR))
        except OSError as e:
            print('boot: rollback failed:', e)
    else:
        print('boot: no rollback possible (writable=%s, backup=%s)'
              % (writable, rollback.have_backup()))

# Deliberately not reset here — only a healthy run in code.py clears this.
rollback.set_failed_boots(failed + 1)
