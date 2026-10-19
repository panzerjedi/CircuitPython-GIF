# CLAUDE.md

Project context for two CircuitPython scripts that run animated GIFs on a pair of
Adafruit Matrix Portal S3 boards, each driving a 128x64 HUB75 LED panel. The boards
act as the two "eyes" of a Halloween pumpkin and stay in sync over ESP-NOW.

## Files

| File on the board | Source name | Purpose |
|---|---|---|
| `code.py` | `code_synced.py` | The real program. Plays every GIF in `/gifs`, in sync with the other board. |
| `code.py` (temporary) | `espnow_test_display.py` | Radio diagnostic. Prints link status on the LED panel. Swap in for `code.py` only while troubleshooting, then restore the real one. |
| `code.py` (temporary) | `calibrate.py` | Button-driven visible-area calibration. Prints the `USABLE_*` / `OFFSET_*` values to paste into the real `code.py`. |
| (runs on a computer) | `fit_gifs.py` | Resizes GIFs to the usable area with Pillow. Not a board script. |

The board-side scripts must be saved as `code.py` on the CIRCUITPY drive to run. Keep a backup of
the real one before swapping in a test or calibration script. `fit_gifs.py` is different: it runs on
a computer, not on the board.

## Hardware and environment

- 2x Adafruit Matrix Portal S3 (ESP32-S3), one per panel. One is the **leader**, one the **follower**.
- 2x 128x64 HUB75 panels with 5 address lines (`board.MTX_ADDRA` to `MTX_ADDRE`).
- CircuitPython **9.x or newer** (needs built-in `gifio` and `espnow`).
- No external libraries. Everything used is built in: `rgbmatrix`, `framebufferio`,
  `displayio`, `gifio`, `espnow`, `wifi`, `terminalio`.
- Each panel needs its own 5V supply (4A minimum, 8-10A is safer). USB cannot power it.

## code.py (code_synced.py)

### Settings (top of file)

| Setting | Default | Meaning |
|---|---|---|
| `ROLE` | `"leader"` | `"leader"` on one board, `"follower"` on the other. The only line that differs between boards. |
| `GIF_FOLDER` | `"/gifs"` | Folder of GIFs, played in alphabetical order. |
| `LOOPS_PER_GIF` | `2` | Full loops per GIF before advancing. The leader decides; the follower obeys. |
| `STARTUP_WAIT` | `60` | Seconds the leader waits for the follower at power-up. `None` waits forever. |
| `FOLLOWER_WAIT` | `10` | Seconds the leader waits before each loop once the follower has been seen. |
| `ABSENT_WAIT` | `1.5` | Short wait used when the follower is missing, so the leader still plays alone and picks the follower up when it appears. |
| `LEADER_TIMEOUT` | `15` | Follower: seconds to wait for the leader at each loop boundary while in sync, before playing on by itself. Keep it longer than the leader's loop-to-loop gap. |
| `ALONE_WAIT` | `2` | Follower: boundary wait once the leader has been lost. |
| `SYNC_TIMEOUT` | `12` | Follower: after sending READY, give up waiting for SYNC after this long. |
| `AUTO_RESET` | `True` | Reboot the board if the program raises an error (otherwise the panel freezes on the last frame). |
| `DEBUG` | `True` | Print received radio messages to the serial console. Set `False` when stable. |
| `BIT_DEPTH` | `3` | Lower (2) reduces flicker, higher gives smoother color. |
| `USABLE_WIDTH` / `USABLE_HEIGHT` | `128` / `64` | Visible pixels when part of the panel is hidden (door frame, bezel). Only used to warn about oversized GIFs; the GIFs themselves must be resized to this size (see below). |
| `OFFSET_X` / `OFFSET_Y` | `0` / `0` | Nudge the picture right/down (+) or left/up (-) so it is centered in the visible area when the hidden margins are uneven. |
| `RGB_PINS` | `B1,G1,R1,B2,G2,R2` | **Red and blue are swapped for these panels.** Do not "fix" this back to R,G,B. |

### How playback works

- `load_gif(path)` frees the old `OnDiskGif`, loads the new one, decodes frame 0 (not shown yet).
- `play_one_loop(delay)` shows frame 0, then decodes the next frame ahead of time and waits on
  absolute deadlines (`time.monotonic_ns`), so timing does not drift. The panel only changes on
  `display.refresh()` (`auto_refresh=False`), so decoding ahead is safe.
- The GIF wraps automatically, so after a full loop frame 0 is already decoded for the next one.
- Frame delays come from the GIF, so every GIF's total duration is what keeps the boards aligned.

### Sync protocol (handshake before every loop)

All messages are ASCII, sent as ESP-NOW **broadcast**.

```
leader   -> PREP:<session>:<n>:<gif_index>:<loop>  "load GIF #idx, sit on frame 0" (resent every 200 ms)
follower -> READY:<session>:<n>                    "loaded and waiting"            (resent every 200 ms)
leader   -> SYNC:<session>:<n>                     "go"  (sent twice; leader starts immediately)
```

- `session` is a random id the leader picks at every boot. `n` is a boundary counter that
  restarts at 0 whenever the leader restarts. The follower treats a `PREP` as new if its session
  differs **or** `n` is higher than the last loop it played; the leader only accepts `READY` for
  its own session and `n`.
- The follower follows the leader's GIF index (modulo its own file count), so a late-booting
  follower catches up at the next loop boundary.
- If the leader times out waiting, it plays alone and keeps retrying each loop.
- Expected sync error between boards is a few milliseconds (radio latency).

### Follower fallback (leader missing or restarted)

The follower never waits forever:

- No `PREP` within `LEADER_TIMEOUT` (or `ALONE_WAIT` once already alone): it plays a loop on its own,
  counts `LOOPS_PER_GIF` itself, and advances through its own GIF list, wrapping at the end.
- While playing alone it checks the radio every frame. When the leader's `PREP` appears it abandons
  the loop, reloads the GIF at frame 0, and re-joins at once.
- If it says READY but no `SYNC` arrives within `SYNC_TIMEOUT` (lost packets, leader gone), it
  gives up and plays on.
- A restarted leader has a new session id, so the follower follows it again immediately.
- Any uncaught error reboots the board when `AUTO_RESET` is on (a CircuitPython error otherwise
  halts the program and leaves the panel frozen).

### Requirements on the GIF folders

- Both boards must hold the **same number of GIFs**, sorted into matching pairs.
  Use identical filenames in both folders (e.g. `01_looking.gif` on each board).
- Each board's GIF is 128x64. The two-panel scene (`10_happy_halloween.gif`) is a 256x64
  animation split into a left half and a right half, so the left file goes on the viewer's-left board.
- Filenames starting with `.` (macOS `._*` files) are ignored.

### Visible area (door frame / bezel)

`load_gif()` centers every GIF on the panel: `tile.x = (PANEL_WIDTH - gif_width) // 2 + OFFSET_X`
(same for y). A GIF smaller than the panel is therefore centered with black around it, and a GIF
larger than the panel is cropped evenly. The board cannot scale a GIF, so to use only the visible area:

1. Save `calibrate.py` as `code.py` on one board. UP moves the highlighted (red) edge inward one pixel
   (hold to repeat); DOWN picks the next edge (LEFT, RIGHT, TOP, BOTTOM). Push each edge in until its
   line is just visible. The panel and serial console then show `USABLE_WIDTH`, `USABLE_HEIGHT`,
   `OFFSET_X`, `OFFSET_Y`. `W = 128 - left - right`, `H = 64 - top - bottom`,
   `OFFSET_X = int((left - right) / 2)`, `OFFSET_Y = int((top - bottom) / 2)`.
2. Put those four values in `code.py` on both boards (each panel may need its own, if the door frame
   hides different amounts).
3. Resize the GIFs to W x H on a computer:
   `python3 fit_gifs.py --width W --height H -o fitted left/ right/`
   Modes: `fit` (default, keeps proportions, black bars), `crop`, `stretch`. Frame timing is preserved.
4. Copy the fitted GIFs onto the boards in place of the originals.

Caveat: `10_happy_halloween.gif` is one 256x64 scene split across two panels. Shrinking each half
independently leaves a gap at the seam and breaks the alignment, so regenerate that scene at the
usable size instead of running it through `fit_gifs.py`.

## espnow_test_display.py

A diagnostic that needs no serial console. It shows four lines on the panel (built-in
`terminalio.FONT`, drawn as a `TileGrid`, so no `adafruit_display_text` library needed):

```
<this board's MAC address>
TX:<sent> RX:<received> E:<error count>
GOT:<last message>        (or the error text, paging every 3 s, when a send fails)
CP<version> WIFI:<none|SET|CONN>
```

How to read it: `RX` climbing on both boards means the link works. `WIFI:SET` or `CONN` means
Wi-Fi credentials are active (see gotchas). `E:` counts errors; the exact error text replaces the
bottom two lines while sends are failing. It starts the radio **before** creating the display.

## Gotchas learned the hard way

1. **ESP-NOW sends must name the peer.** Register the broadcast peer, keep the object, and call
   `radio.send(msg, peer)`. Calling `radio.send(msg)` with no peer fails with error `0x3069`
   (`ESP_ERR_ESPNOW_NOT_FOUND`, "peer not found"). Both scripts use `BPEER` / `bpeer` for this.
2. **Wi-Fi credentials break ESP-NOW.** If `settings.toml` has `CIRCUITPY_WIFI_SSID` /
   `CIRCUITPY_WIFI_PASSWORD`, the board joins the router and moves to its channel, so the two
   boards cannot hear each other. Comment those lines out on **both** boards.
3. **Clear the panel at startup.** Until the first `display.refresh()`, the matrix shows
   uninitialised memory (garbled). `code.py` clears to black immediately and shows frame 0 while waiting.
4. **Red and blue are swapped on these panels.** Keep the `B1,G1,R1,B2,G2,R2` pin order.
   If the second panel shows wrong colors, check its wiring separately.
5. **Both boards must run the same version of `code.py`.** The handshake message format changed
   over time (it now includes a session id); mismatched versions will not understand each other.
6. **The old follower froze after any leader restart.** It ignored every `PREP` whose counter was not
   higher than the last one it played, and a restarted leader counts from 0 again. Fixed with the
   session id above. If the follower freezes in a future version, check this logic first.
7. **Mismatched GIF counts look like a "stuck" follower.** The follower maps the leader's index with
   `% its own count`, so if it has fewer files it repeats early GIFs, and if it has more it never
   shows the extras. Check both `gifs` folders have the same number of files.
8. **Do not use `time.monotonic()` for long-running timing.** Its precision degrades with uptime.
   Use `time.monotonic_ns()` (as the code does).

## Making GIFs that run smoothly

The board decodes each frame from flash while refreshing the panel, so GIFs that change every
pixel every frame cause flicker. What works:

- Mostly static frames where only a small area changes (moving pupil, edges, small sprites).
- Black backgrounds. They draw less power and avoid brightness sag.
- 32 colors or fewer, shared across all frames, no dithering.
- Banded (posterized) glows instead of smooth gradients; **no per-frame brightness flicker**.
- About 20 fps, with durations that are exact multiples of the frame delay.
- Keep the eye centered on its own 128x64 panel; left and right files are mirror images with
  the same gaze direction.

## Troubleshooting quick reference

| Symptom | Likely cause / fix |
|---|---|
| Garbled panel while waiting | Old code that never drew before the handshake. Use the current `code.py`. |
| Leader plays but follower never starts | Radio link down. Run `espnow_test_display.py`; check `WIFI:` and `RX`. |
| `E:` rising, error `0x3069` | `send()` called without naming the peer. See gotcha 1. |
| Wrong colors | `RGB_PINS` order. See gotcha 4. |
| Flicker on certain GIFs | Heavy GIF (see GIF guidance), `BIT_DEPTH` too high, or weak power supply. |
| Boards drift out of step mid-loop | Check both GIFs have the same total duration; they re-sync every loop. |
