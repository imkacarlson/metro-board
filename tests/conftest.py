"""Make metro_api importable on plain CPython.

The board's runtime provides `rtc` and `adafruit_bitmap_font`, and config.py
loads a font and reads the API key at import time. Stub all of it before the
first `import metro_api` so the tests never need hardware.

These tests are development-only: deploy.yml publishes exactly code.py,
config.py, metro_api.py and train_board.py, so tests/ never reaches the board.
"""
import os
import sys
import types

# Appended, not prepended: the repo root holds a code.py, and putting it ahead
# of the stdlib shadows the `code` module that pdb imports.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('WMATA_API_KEY', 'test-key')


def _stub(name, **attrs):
    if name in sys.modules and all(hasattr(sys.modules[name], k) for k in attrs):
        return
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


_stub('rtc', RTC=lambda: types.SimpleNamespace(datetime=None))

# CPython has a stdlib `secrets`, but it has no `secrets` dict for metro_api to
# import, so this one has to be forced into place.
_secrets = types.ModuleType('secrets')
_secrets.secrets = {}
sys.modules['secrets'] = _secrets

_bitmap_font = types.ModuleType('adafruit_bitmap_font.bitmap_font')
_bitmap_font.load_font = lambda path: object()
_stub('adafruit_bitmap_font', bitmap_font=_bitmap_font)
sys.modules['adafruit_bitmap_font.bitmap_font'] = _bitmap_font
