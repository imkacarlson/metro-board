"""One-shot over-the-air updater.

Runs *instead of* code.py, handed control by supervisor.set_next_code_file().
That indirection is the whole point: here the matrix and the app's network stack
do not exist yet, so there is ~65 KB of free heap instead of the ~18 KB the
running app leaves. A 26 KB download does not fit in the latter.

Like boot.py, this file is not itself over-the-air updatable — an updater that
can overwrite itself has no floor to stand on.

Control always returns to code.py, whether the update succeeded or not.
"""
import gc
import os
import json
import supervisor
import microcontroller

# Pinned. Owner, repo and branch are fixed here and the manifest is never
# allowed to contribute any part of a URL — see _install().
OWNER_REPO = 'imkacarlson/metro-board'
BRANCH = 'deploy'
BASE_URL = 'https://raw.githubusercontent.com/%s/%s/' % (OWNER_REPO, BRANCH)
MANIFEST_URL = BASE_URL + 'manifest.json'

# Only these four files are ever written. boot.py, ota.py, safemode.py,
# secrets.py and settings.toml are not updatable by design.
ALLOWED_FILES = ('code.py', 'config.py', 'metro_api.py', 'train_board.py')

MAX_FILES = len(ALLOWED_FILES)
MAX_FILE_BYTES = 60000
MAX_TOTAL_BYTES = 200000
MAX_VERSION_LEN = 40
CHUNK = 512

BACKUP_DIR = '/backup'
VERSION_FILE = '/version.txt'


class OtaError(Exception):
    pass


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
                chunk = fsrc.read(CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)


def _read_line(path):
    try:
        with open(path, 'r') as f:
            return f.read(64).strip()
    except OSError:
        return ''


def _connect():
    import board
    import neopixel
    from adafruit_matrixportal.network import Network

    status_pixel = neopixel.NeoPixel(board.NEOPIXEL, 1)
    status_pixel[0] = (0, 0, 0)
    del status_pixel

    network = Network(status_neopixel=None)
    gc.collect()
    return network


def _fetch(network, url):
    """GET a URL, insisting on a literal 200.

    Nothing else is honoured, including redirects: the frozen adafruit_requests
    build does not expose its redirect policy for inspection, and the pinned raw
    URL is known to answer 200 directly.
    """
    response = network.fetch(url)
    if response.status_code != 200:
        code = response.status_code
        response.close()
        raise OtaError('%s returned HTTP %d' % (url, code))
    return response


def _valid_path(path):
    """Manifest paths are allowlisted, then re-checked for traversal anyway.

    The allowlist alone is sufficient; the explicit checks are here so that
    widening the allowlist later cannot quietly reintroduce a path-traversal
    write to, say, ../../boot.py.
    """
    if not isinstance(path, str) or path not in ALLOWED_FILES:
        return False
    if '..' in path or '/' in path or '\\' in path or path.startswith('/'):
        return False
    return True


def _parse_manifest(body):
    manifest = json.loads(body)

    version = manifest.get('version')
    if not isinstance(version, str) or not version or len(version) > MAX_VERSION_LEN:
        raise OtaError('manifest has no usable version')

    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
        raise OtaError('manifest file list is missing or oversized')

    files = []
    seen = []
    total = 0
    for entry in entries:
        path = entry.get('path')
        size = entry.get('size')
        if not _valid_path(path):
            raise OtaError('manifest path rejected: %r' % (path,))
        if path in seen:
            raise OtaError('manifest lists %s twice' % path)
        if not isinstance(size, int) or size <= 0 or size > MAX_FILE_BYTES:
            raise OtaError('manifest size rejected for %s: %r' % (path, size))
        total += size
        if total > MAX_TOTAL_BYTES:
            raise OtaError('manifest exceeds %d total bytes' % MAX_TOTAL_BYTES)
        seen.append(path)
        files.append((path, size))

    return version, files


def _back_up(files):
    if not _exists(BACKUP_DIR):
        os.mkdir(BACKUP_DIR)
    for path, _size in files:
        if _exists('/' + path):
            _copy('/' + path, BACKUP_DIR + '/' + path)
    if _exists(VERSION_FILE):
        _copy(VERSION_FILE, BACKUP_DIR + VERSION_FILE)
    print('ota: backed up current version to', BACKUP_DIR)


def _download(network, path, expected):
    """Stream one file to <path>.tmp and verify its byte length."""
    tmp = '/' + path + '.tmp'
    response = _fetch(network, BASE_URL + path)
    written = 0
    try:
        with open(tmp, 'wb') as f:
            for chunk in response.iter_content(CHUNK):
                if not chunk:
                    continue
                written += len(chunk)
                if written > expected:
                    raise OtaError('%s is longer than the manifest claims' % path)
                f.write(chunk)
                # Mandatory, not defensive. Measured on this board while
                # streaming a 26 KB file: 64,064 bytes free with this call,
                # 2,976 bytes free without it.
                gc.collect()
    finally:
        response.close()
        gc.collect()

    actual = os.stat(tmp)[6]
    if actual != expected:
        raise OtaError('%s is %d bytes, manifest says %d' % (path, actual, expected))
    print('ota: fetched %s (%d bytes)' % (path, actual))


def _discard(files):
    for path, _size in files:
        try:
            os.remove('/' + path + '.tmp')
        except OSError:
            pass


def _commit(files, version):
    """Swap every verified .tmp into place, then record the new version.

    Nothing is swapped until every file has downloaded and verified, so a failed
    download leaves the board untouched. A power cut *during* the swap leaves a
    half-installed set, which is exactly the case boot.py's rollback covers.
    """
    for path, _size in files:
        try:
            os.remove('/' + path)
        except OSError:
            pass
        os.rename('/' + path + '.tmp', '/' + path)

    with open(VERSION_FILE, 'w') as f:
        f.write(version)
    print('ota: installed version', version)


def _install():
    if supervisor.runtime.usb_connected:
        print('ota: USB connected — filesystem is read-only, nothing to do')
        return

    network = _connect()

    response = _fetch(network, MANIFEST_URL)
    try:
        body = response.text
    finally:
        response.close()
        gc.collect()

    version, files = _parse_manifest(body)
    del body
    gc.collect()

    local = _read_line(VERSION_FILE)
    if version == local:
        print('ota: already on', version)
        return

    print('ota: %s -> %s (%d files)' % (local or 'unknown', version, len(files)))

    _back_up(files)
    try:
        for path, size in files:
            # The URL is built from the pinned constants and the allowlisted
            # filename. The manifest never supplies a URL of any kind.
            _download(network, path, size)
    except Exception:
        _discard(files)
        raise

    _commit(files, version)


try:
    _install()
except Exception as e:
    print('ota: update abandoned, staying on current version:', e)

# Hard reset rather than supervisor.reload(). boot.py only runs on a hard reset
# (main.c calls run_boot_py() outside the run loop), and it is boot.py that arms
# recover.py — so a soft reload here would hand control to a freshly installed
# code.py with no recovery hook, which is precisely when one is needed.
print('ota: resetting into code.py')
microcontroller.reset()
