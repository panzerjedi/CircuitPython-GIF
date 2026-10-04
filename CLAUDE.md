# CLAUDE.md

Project context for two CircuitPython scripts that run animated GIFs on a pair of
Adafruit Matrix Portal S3 boards, each driving a 128x64 HUB75 LED panel. The boards
act as the two "eyes" of a Halloween pumpkin and stay in sync over ESP-NOW.

## Files

| File on the board | Source name | Purpose |
|---|---|---|
| `code.py` | Plays every GIF in `/gifs`, in sync with the other board. |
| `espnow_test_display.py` | Radio diagnostic. Prints link status on the LED panel. Swap in for `code.py` only while troubleshooting, then restore the real one. |

Both scripts must be saved as `code.py` on the CIRCUITPY drive to run. Keep a backup of
the real one before swapping in the test.

## Hardware and environment

- 2x Adafruit Matrix Portal S3 (ESP32-S3), one per panel. One is the **leader**, one the **follower**.
- 2x 128x64 HUB75 panels with 5 address lines (`board.MTX_ADDRA` to `MTX_ADDRE`).
- CircuitPython **9.x or newer** (needs built-in `gifio` and `espnow`).
- No external libraries. Everything used is built in: `rgbmatrix`, `framebufferio`,
  `displayio`, `gifio`, `espnow`, `wifi`, `terminalio`.
- Each panel needs its own 5V supply (4A minimum, 8-10A is safer). USB cannot power it.

## code.py

### Settings (top of file)

| Setting | Default | Meaning |
|---|---|---|
| `ROLE` | `"leader"` | `"leader"` on one board, `"follower"` on the other. The only line that differs between boards. |
| `GIF_FOLDER` | `"/gifs"` | Folder of GIFs, played in alphabetical order. |
| `LOOPS_PER_GIF` | `2` | Full loops per GIF before advancing. The leader decides; the follower obeys. |
| `STARTUP_WAIT` | `60` | Seconds the leader waits for the follower at power-up. `None` waits forever. |
| `FOLLOWER_WAIT` | `10` | Seconds the leader waits before each loop once the follower has been seen. |
| `ABSENT_WAIT` | `0.3` | Short wait used when the follower is missing, so the leader still plays alone and picks the follower up when it appears. |
| `DEBUG` | `True` | Print received radio messages to the serial console. Set `False` when stable. |
| `BIT_DEPTH` | `2` | Lower (2) reduces flicker, higher gives smoother color (max 5). |
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
leader   -> PREP:<n>:<gif_index>:<loop>   "load GIF #idx, sit on frame 0" (resent every 200 ms)
follower -> READY:<n>                      "loaded and waiting"            (resent every 200 ms)
leader   -> SYNC:<n>                       "go"  (sent twice; leader starts immediately)
```

- `n` is a boundary counter. It lets each side ignore stale or duplicate messages
  (the follower ignores any `PREP` with `n <= last_n`; the leader only accepts `READY:<its n>`).
- The follower holds no GIF logic of its own. It loads whichever index the leader names
  (modulo its own file count), so a late-booting follower catches up at the next loop boundary.
- If the leader times out waiting, it plays alone and keeps retrying each loop.
- Expected sync error between boards is a few milliseconds (radio latency).

### Requirements on the GIF folders

- Both boards must hold the **same number of GIFs**, sorted into matching pairs.
  Use identical filenames in both folders (e.g. `01_looking.gif` on each board).
- Each board's GIF is 128x64. The two-panel scene (`10_happy_halloween.gif`) is a 256x64
  animation split into a left half and a right half, so the left file goes on the viewer's-left board.
- Filenames starting with `.` (macOS `._*` files) are ignored.

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
   over time; mismatched versions will both sit waiting.
6. **Do not use `time.monotonic()` for long-running timing.** Its precision degrades with uptime.
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
