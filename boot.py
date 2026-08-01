"""Anchor file — deliberately NOT over-the-air updatable.

This file does as little as possible, on purpose. Two hard-won constraints from
CircuitPython's main.c explain why:

  * `supervisor.runtime.usb_connected` is tud_ready(), and the USB stack is not
    started until port_post_boot_py() — *after* boot.py. So it always reads
    False here, even on a computer. Remounting the filesystem based on it made
    the board writable to Python and therefore read-only to the host, silently
    killing drag-and-drop on every boot. The remount now happens in code.py,
    where USB is up and the answer is real.

  * `supervisor.set_next_code_file()` puts its filename at the front of the
    search list unconditionally (main.c ~L461). reload_on_error controls
    stickiness, NOT whether the file is used. Arming a recovery file here meant
    it ran *instead of* code.py on every boot, so the app never ran at all.

What is left is the boot counter, which needs no filesystem access.
"""
import microcontroller

NVM_HEALTH_SLOT = 0

_n = microcontroller.nvm[NVM_HEALTH_SLOT]
if _n == 255:          # erased flash reads as 0xFF
    _n = 0
if _n < 254:
    microcontroller.nvm[NVM_HEALTH_SLOT:NVM_HEALTH_SLOT + 1] = bytes([_n + 1])

# Goes to serial and to boot_out.txt. Cannot go to /board.log: the filesystem
# is still read-only to Python at this point, by design.
print('boot: consecutive boots without a healthy run =', _n)
