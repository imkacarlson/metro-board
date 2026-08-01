# DC Metro Board

A CircuitPython arrival board for a 64x32 RGB matrix driven by an Adafruit
MatrixPortal M4. It shows the next three DC Metro trains from Dupont Circle
(A03) across both directions, and during the weekday morning commute it works
out whether a southbound Red Line train actually connects to anything at Metro
Center.

It updates itself over the air: push to `main`, and the board picks up the new
code on its next check.

## Hardware

- Adafruit MatrixPortal M4
- 64x32 RGB LED matrix
- CircuitPython 9.x

The ESP32 on this board is a co-processor, so `wifi`, `ssl` and `hashlib` are
unavailable to CircuitPython and the standard **Web Workflow does not run
here**. That is why updates are hand-rolled rather than using Adafruit's usual
answer to remote code editing.

## Setup

1. Copy `settings.toml.template` to `settings.toml` on the `CIRCUITPY` drive and
   fill in your WiFi credentials and a [WMATA API key][wmata]. Do the same for
   `secrets.py.template` → `secrets.py`. Both are gitignored and neither is ever
   part of an update.
2. Install the CircuitPython libraries into `lib/`: `adafruit_matrixportal`,
   `adafruit_display_text`, `adafruit_display_shapes`, `adafruit_bitmap_font`,
   plus the `5x7.bdf` font.
3. Copy `boot.py`, `recover.py`, `rollback.py`, `ota.py` and the four
   application files onto the board, plus a `version.txt` containing the short
   SHA currently published on the `deploy` branch.

`code.py` imports `rollback`, so `rollback.py` must be present on the board even
though it is never updated over the air.

[wmata]: https://developer.wmata.com/

## Over-the-air updates

```
push to main  ->  Action publishes deploy branch  ->  board polls  ->  ota.py installs
```

A push to `main` triggers `.github/workflows/deploy.yml`, which computes a
manifest (version = the short commit SHA, plus each file's byte size) and
publishes it next to the four application files on a dedicated `deploy` branch.
Publishing to `deploy` rather than committing back to `main` is what keeps the
workflow from re-triggering itself.

The board fetches
`https://raw.githubusercontent.com/imkacarlson/metro-board/deploy/manifest.json`
after its first successful display refresh, once every 24 hours, and on demand
whenever the **UP button** is pressed. `raw.githubusercontent.com` caches for
about five minutes, which is irrelevant at a daily poll and barely noticeable
after a button press.

If the published version differs from `/version.txt`, `code.py` hands control to
`ota.py` via `supervisor.set_next_code_file()` and reboots. `ota.py` runs before
the matrix and the app's network stack exist, where there is roughly 65 KB of
free heap instead of the ~18 KB the running app leaves — a 26 KB download does
not fit in the latter. It backs up the current files, streams each new file to
`<name>.tmp` in 512-byte chunks, verifies the byte length against the manifest,
and only once **every** file has downloaded and verified does it rename them
into place. Any failure at any step discards the `.tmp` files and leaves the
board on the version it was already running.

### Which files update

| File | Updatable | Why |
|---|---|---|
| `code.py`, `config.py`, `metro_api.py`, `train_board.py` | yes | the application |
| `boot.py`, `recover.py`, `rollback.py` | **no** | they *are* the recovery path; nothing remote may disable it |
| `ota.py` | **no** | an updater that can overwrite itself has no floor to stand on |
| `secrets.py`, `settings.toml` | **no** | credentials, never published |

Changing any of those means plugging the board in. That is the point.

### Rollback

The important constraint is one most CircuitPython write-ups gloss over:
**`boot.py` runs only once per hard reset.** `main.c` calls `run_boot_py()`
outside the run loop, so `supervisor.reload()` never comes back through it. Two
things follow, and the design leans on both.

First, the writable remount `boot.py` performs survives the soft reload into
`ota.py` — which is the only reason `ota.py` can write files at all.

Second, a boot counter on its own is not enough. If a freshly downloaded
`code.py` fails at *import*, nothing in `boot.py` ever runs again, and the board
would sit dead until somebody power-cycled it three times. So `boot.py` arms
CircuitPython's own mechanism instead:

```python
supervisor.set_next_code_file('recover.py', reload_on_error=True, sticky_on_error=True)
```

When `code.py` exits with an exception, the board reloads into `recover.py`,
which restores `/backup` over the live files, records the version that broke in
`/ota_blocked.txt`, and hard-resets. `code.py` then refuses to install that
version again — without the quarantine the board would roll back, immediately
re-download the same broken build, and loop. It sits on the last good version
until a *different* one is published.

The hook is armed only when the filesystem is writable, i.e. only when running
standalone. A syntax error while tethered leaves the traceback on screen for you
to read instead of resetting it away.

The boot counter remains as a backstop for failures that never raise — a hang,
or a crash that resets the board outright. It has exactly one rule: **only a
healthy run zeroes it.** `boot.py` increments it, `recover.py` reads it to
decide whether to keep trying, and `code.py` zeroes it once the display has
actually refreshed. That invariant is what makes a permanently broken build halt
after a few attempts rather than thrash the flash forever.

Recovery ladder if all of that somehow fails: single reset reboots; double-press
reset enters safe mode, which skips both `boot.py` and `code.py` and restores
the USB drive; double-tap reset reaches the UF2 bootloader in protected ROM;
worst case, reflash CircuitPython. **The board cannot be permanently bricked.**

## Security model

The board never listens. No open ports, no inbound connections, no broker — only
outbound HTTPS to one pinned URL. "Someone pushing to the board" is impossible
by construction. The only real question is whether what sits at that URL can be
tampered with.

**TLS impersonation is closed**, verified on-device against badssl.com. Expired,
self-signed, untrusted-root and *wrong-hostname* certificates are all rejected,
with a valid same-host certificate accepted as a control. The wrong-hostname
rejection is the important one: it proves the ESP32 firmware verifies hostname,
not just chain and expiry, so DNS hijacking, ARP spoofing and hostile WiFi all
fail. An attacker would need a real CA mis-issuance for
`raw.githubusercontent.com`.

So the trust boundary is: **whoever can write to the `deploy` branch controls the
board.** In practice that means the GitHub account.

- **Enable 2FA on the GitHub account.** This is the single most meaningful
  control in the whole design. Audit personal access tokens too — any token with
  write access to this repo is equivalent to full control of the board.
- The workflow is hardened: `push: branches: [main]` only, never
  `pull_request_target`; zero third-party actions; `permissions: contents:
  write` and nothing else; no secrets. Anyone can fork this repo and open a PR,
  but fork PRs get a read-only token and cannot deploy. Only a merge to `main`
  publishes.
- Branch protection on `main` and `deploy` as defence in depth.

On the board: manifest paths are checked against an allowlist and rejected if
they contain `..`, a leading `/` or a backslash; the manifest never supplies a
URL of any kind, only filenames appended to the pinned base; only a literal
HTTP 200 is honoured, including no redirects; and per-file, total-byte and
file-count caps mean a runaway or hostile manifest cannot fill the flash.

**There is no signature verification.** `hashlib` is absent on this board and
pure-Python SHA-256 is too slow and too memory-hungry at 18–65 KB free to
justify. Trust rests on TLS plus GitHub account security. Stated plainly rather
than papered over.

Residual risks, honestly:

- If the GitHub account or GitHub itself is compromised, the board runs attacker
  code, and malicious code could read the WiFi credentials off `secrets.py`.
  2FA is the mitigation. This is the accepted residual risk.
- **Never rename the GitHub account.** The board's URL is pinned to
  `imkacarlson/metro-board`. Rename it and the old namespace becomes claimable
  by a stranger, who could then serve code to the board.
- If the repo is deleted, the fetch 404s and the board fails safe — it keeps
  running what it has.
- Physical access is out of scope. Anyone holding the board can do anything.

## Metro logic

Outside the weekday 06:45–08:30 window, the board runs "simple mode": one
request to Dupont, both directions, next three trains at least 7 minutes out
(the walk to the station).

Inside the window it makes one combined request for four stations and merges:

- **Southbound (Glenmont)** from Tenleytown (`A07`) plus a 7-minute ride offset,
  so predictions extend further out than Dupont's own board reaches.
- **Northbound** straight off Dupont's own board, selected on WMATA's `Group`
  field (`Group 2` is northbound at A03) with no destination filter.
- **Onward eastbound connections** at Metro Center from Clarendon (`K02`) and
  Pentagon (`C07`), used to decide whether each Glenmont train is worth taking
  or should blink "skip".

Selecting northbound on `Group` rather than on a destination string is
deliberate. The previous version sourced northbound from NoMa (`B35`) filtered
on `Destination in ('Shady Grove', 'Shady Grv')`. Under the 2025 summer shutdown
those trains terminate at Friendship Heights, so the filter matched nothing and
the row silently vanished during exactly the window it mattered. `Group` is the
direction and does not change when the service pattern does.

`?TrainGroup=` is not sent: WMATA silently ignores it (verified — identical
30-train responses with and without it), and direction has always been decided
on this end.

## Files

| File | Purpose |
|---|---|
| `code.py` | Main loop, update checks, boot-health reset |
| `config.py` | Configuration and display tokens |
| `metro_api.py` | WMATA fetch, streaming parser, transfer logic |
| `train_board.py` | Matrix display |
| `boot.py` | Mount decision, arms the recovery hook (never OTA-updated) |
| `recover.py` | Runs when `code.py` throws; rolls back (never OTA-updated) |
| `rollback.py` | Shared restore/quarantine helpers (never OTA-updated) |
| `ota.py` | The updater itself (never OTA-updated) |
| `.github/workflows/deploy.yml` | Publishes the `deploy` branch |

## Serial console

Connect at **115200 baud** to the board's COM port (Device Manager → Ports) with
PuTTY or any serial terminal. All the OTA and prediction logic logs there.
