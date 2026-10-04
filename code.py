# Matrix Portal S3 + 128x64 panel: synchronized multi-GIF player (two boards via ESP-NOW)
# Rename this file to code.py on BOTH boards. Set ROLE on each.
# Requires CircuitPython 9.x or newer. No extra libraries needed.
#
# Both boards must have the SAME NUMBER of GIFs in /gifs, with filenames that sort
# into matching pairs (same names on both boards works best).
#
# How the sync works (handshake before every loop):
#   leader  -> "PREP"  : "get GIF #idx ready at frame 0"
#   follower-> "READY" : "loaded and waiting"
#   leader  -> "SYNC"  : "go!"   (leader starts at the same moment)
# The leader will not start a loop until the follower says it is ready.

import gc
import os
import time
import board
import displayio
import framebufferio
import rgbmatrix
import gifio
import espnow

# ---------------- Settings ----------------
ROLE = "leader"          # "leader" on one board, "follower" on the other
GIF_FOLDER = "/gifs"
LOOPS_PER_GIF = 2        # full loops of each GIF before moving to the next (leader decides)

# How long the LEADER waits for the follower (seconds).
STARTUP_WAIT = 60        # at power-up. Use None to wait forever.
FOLLOWER_WAIT = 10       # before each loop once the follower has been seen
ABSENT_WAIT = 0.3        # before each loop if the follower is missing (so the leader
                         # still plays on its own, and picks the follower up when it appears)

DEBUG = True             # print radio messages to the serial console

PANEL_WIDTH = 128
PANEL_HEIGHT = 64
BIT_DEPTH = 2

# Red/blue swapped to match this panel's wiring
RGB_PINS = [
    board.MTX_B1, board.MTX_G1, board.MTX_R1,
    board.MTX_B2, board.MTX_G2, board.MTX_R2,
]
# ------------------------------------------

# ---- Panel setup ----
displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=PANEL_WIDTH,
    height=PANEL_HEIGHT,
    bit_depth=BIT_DEPTH,
    rgb_pins=RGB_PINS,
    addr_pins=[
        board.MTX_ADDRA, board.MTX_ADDRB, board.MTX_ADDRC,
        board.MTX_ADDRD, board.MTX_ADDRE,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
    doublebuffer=True,
)
display = framebufferio.FramebufferDisplay(matrix, auto_refresh=False)
display.root_group = displayio.Group()
display.refresh()                      # clear to black right away (no garbage while waiting)

# ---- ESP-NOW setup ----
radio = espnow.ESPNow()
BPEER = espnow.Peer(mac=b"\xff\xff\xff\xff\xff\xff")   # broadcast to everyone in range
radio.peers.append(BPEER)


def send(msg):
    try:
        radio.send(msg.encode(), BPEER)      # send to the broadcast peer explicitly
    except Exception as e:
        print("send failed:", e)


def poll():
    """Return the next received message as a string, or None."""
    pkt = radio.read()
    if pkt is None:
        return None
    try:
        msg = pkt.msg.decode()
    except Exception:
        return None
    if DEBUG:
        print("RX:", msg)
    return msg


def drain_radio():
    while radio.read() is not None:
        pass


# ---- GIF handling ----
def find_gifs(folder):
    try:
        files = os.listdir(folder)
    except OSError:
        return []
    return sorted(
        folder + "/" + f
        for f in files
        if f.lower().endswith(".gif") and not f.startswith(".")
    )


odg = None


def load_gif(path):
    """Free the previous GIF and load a new one. Returns the first frame's delay."""
    global odg
    display.root_group = displayio.Group()
    if odg is not None:
        odg.deinit()
        odg = None
    gc.collect()

    odg = gifio.OnDiskGif(path)
    tile = displayio.TileGrid(
        odg.bitmap,
        pixel_shader=displayio.ColorConverter(
            input_colorspace=displayio.Colorspace.RGB565_SWAPPED
        ),
    )
    tile.x = max(0, (PANEL_WIDTH - odg.width) // 2)
    tile.y = max(0, (PANEL_HEIGHT - odg.height) // 2)
    group = displayio.Group()
    group.append(tile)
    display.root_group = group
    print("Loaded", path, "-", odg.frame_count, "frames")
    return odg.next_frame()   # decode frame 0 (not shown until refresh)


def play_one_loop(delay):
    """Show frame 0 now and play through to the end. Returns frame 0's delay
    (the GIF wraps around, so frame 0 is already decoded for the next loop)."""
    display.refresh()
    t = time.monotonic_ns()
    frame_count = odg.frame_count
    for i in range(frame_count):
        next_delay = odg.next_frame()        # decode ahead; panel only changes on refresh()
        t += int(delay * 1e9)                # absolute deadlines: no drift
        remaining = (t - time.monotonic_ns()) / 1e9
        if remaining > 0:
            time.sleep(remaining)
        if i < frame_count - 1:
            display.refresh()
        delay = next_delay
    return delay


# ---- Startup ----
gifs = find_gifs(GIF_FOLDER)
while not gifs:
    print("No GIFs found in", GIF_FOLDER)
    time.sleep(5)
    gifs = find_gifs(GIF_FOLDER)

print("Role:", ROLE, "|", len(gifs), "GIF(s)")
drain_radio()
idx = 0
delay = load_gif(gifs[idx])
display.refresh()                      # show frame 0 while waiting for the other board
RESEND_NS = 200_000_000   # re-send handshake messages every 200 ms


# =====================================================================
#                              LEADER
# =====================================================================
def leader_handshake(n, idx, loop, timeout):
    """Ask the follower to get ready; wait for its READY. True if it answered."""
    start = time.monotonic_ns()
    end = None if timeout is None else start + int(timeout * 1e9)
    next_send = 0
    announced = False
    want = "READY:%d" % n
    while True:
        now = time.monotonic_ns()
        if now >= next_send:
            send("PREP:%d:%d:%d" % (n, idx, loop))
            next_send = now + RESEND_NS
        m = poll()
        if m == want:
            return True
        if end is not None and now >= end:
            return False
        if not announced and now - start > 500_000_000:
            print("Leader: waiting for follower...")
            announced = True
        if m is None:
            time.sleep(0.001)


if ROLE == "leader":
    n = 0
    loop = 0
    present = False
    first = True
    while True:
        if first:
            timeout = STARTUP_WAIT
        elif present:
            timeout = FOLLOWER_WAIT
        else:
            timeout = ABSENT_WAIT
        first = False

        got = leader_handshake(n, idx, loop, timeout)
        if got and not present:
            print("Leader: follower ready")
        if not got and present:
            print("Leader: follower lost, playing alone")
        present = got

        if present:
            send("SYNC:%d" % n)
            send("SYNC:%d" % n)              # sent twice in case one is dropped
        delay = play_one_loop(delay)

        n += 1
        loop += 1
        if loop >= LOOPS_PER_GIF:
            loop = 0
            if len(gifs) > 1:
                idx = (idx + 1) % len(gifs)
                delay = load_gif(gifs[idx])


# =====================================================================
#                             FOLLOWER
# =====================================================================
else:
    last_n = -1
    while True:
        # 1) wait for the leader to tell us what to get ready
        while True:
            m = poll()
            if m and m.startswith("PREP:"):
                try:
                    _, n_s, i_s, _l = m.split(":")
                    n, midx = int(n_s), int(i_s)
                except Exception:
                    continue
                if n > last_n:               # ignore repeats of an old request
                    break
            elif m is None:
                time.sleep(0.001)

        midx %= len(gifs)
        if midx != idx:
            idx = midx
            delay = load_gif(gifs[idx])

        # 2) say READY (repeatedly) until the leader sends SYNC
        next_send = 0
        newest = n
        while True:
            now = time.monotonic_ns()
            if now >= next_send:
                send("READY:%d" % newest)
                next_send = now + RESEND_NS
            m = poll()
            if m is None:
                time.sleep(0.001)
                continue
            if m == "SYNC:%d" % newest:
                break
            if m.startswith("PREP:"):        # leader moved on (e.g. it timed out earlier)
                try:
                    _, n_s, i_s, _l = m.split(":")
                    n2, midx2 = int(n_s), int(i_s) % len(gifs)
                except Exception:
                    continue
                if n2 > newest:
                    newest = n2
                    if midx2 != idx:
                        idx = midx2
                        delay = load_gif(gifs[idx])

        last_n = newest
        delay = play_one_loop(delay)
