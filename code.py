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
#
# Every message carries a random SESSION id that the leader picks at boot, so a
# follower notices when the leader has restarted and follows it again.
# If the leader disappears, the follower keeps cycling through its GIFs on its
# own and re-joins as soon as the leader is back.

import gc
import os
import time
import board
import microcontroller
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
ABSENT_WAIT = 1.5        # before each loop if the follower is missing (so the leader
                         # still plays on its own, and picks the follower up when it appears)

# How the FOLLOWER copes when the leader is missing (seconds).
LEADER_TIMEOUT = 15      # while in sync: wait this long for the leader at each loop boundary
ALONE_WAIT = 2           # once the leader is lost: wait this long at each boundary, then play on
SYNC_TIMEOUT = 12        # after saying READY: give up waiting for the leader's "go" after this
AUTO_RESET = True        # reboot the board if the program hits an error (recommended)

DEBUG = True             # print radio messages to the serial console

PANEL_WIDTH = 128
PANEL_HEIGHT = 64
BIT_DEPTH = 3

# Visible area of the panel (parts of it may be hidden by a door frame, bezel, etc.).
# The usable area is a rectangle centered on the panel. Find the numbers with calibrate.py.
# GIFs smaller than the panel are centered inside it, so resize your GIFs to this size
# with fit_gifs.py (a GIF can't be shrunk on the board itself).
USABLE_WIDTH = 128       # visible pixels across
USABLE_HEIGHT = 64       # visible pixels down
OFFSET_X = 0             # nudge the picture right (+) or left (-), in pixels
OFFSET_Y = 0             # nudge the picture down (+) or up (-), in pixels

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
    # Center the picture on the panel (plus any nudge). A GIF larger than the
    # panel gets a negative position, which crops it evenly on all sides.
    tile.x = (PANEL_WIDTH - odg.width) // 2 + OFFSET_X
    tile.y = (PANEL_HEIGHT - odg.height) // 2 + OFFSET_Y
    if odg.width > USABLE_WIDTH or odg.height > USABLE_HEIGHT:
        print("Note: GIF is %dx%d but the usable area is %dx%d - edges may be hidden. "
              "Run fit_gifs.py." % (odg.width, odg.height, USABLE_WIDTH, USABLE_HEIGHT))
    group = displayio.Group()
    group.append(tile)
    display.root_group = group
    print("Loaded", path, "-", odg.frame_count, "frames")
    return odg.next_frame()   # decode frame 0 (not shown until refresh)


def play_one_loop(delay, on_frame=None):
    """Show frame 0 now and play through to the end. Returns (delay, aborted).
    delay is frame 0's delay (the GIF wraps around, so frame 0 is already decoded
    for the next loop). If on_frame() returns True the loop is cut short and
    aborted is True; the GIF is then mid-way and must be reloaded."""
    display.refresh()
    t = time.monotonic_ns()
    frame_count = odg.frame_count
    for i in range(frame_count):
        next_delay = odg.next_frame()        # decode ahead; panel only changes on refresh()
        t += int(delay * 1e9)                # absolute deadlines: no drift
        remaining = (t - time.monotonic_ns()) / 1e9
        if remaining > 0:
            time.sleep(remaining)
        if on_frame is not None and on_frame():
            return delay, True
        if i < frame_count - 1:
            display.refresh()
        delay = next_delay
    return delay, False


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
SESSION = "%06x" % int.from_bytes(os.urandom(3), "big")   # new id every time the leader boots


def leader_handshake(n, idx, loop, timeout):
    """Ask the follower to get ready; wait for its READY. True if it answered."""
    start = time.monotonic_ns()
    end = None if timeout is None else start + int(timeout * 1e9)
    next_send = 0
    announced = False
    want = "READY:%s:%d" % (SESSION, n)
    while True:
        now = time.monotonic_ns()
        if now >= next_send:
            send("PREP:%s:%d:%d:%d" % (SESSION, n, idx, loop))
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


def run_leader():
    global idx, delay
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
            send("SYNC:%s:%d" % (SESSION, n))
            send("SYNC:%s:%d" % (SESSION, n))    # sent twice in case one is dropped
        delay, _ = play_one_loop(delay)

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
def parse_prep(m):
    """'PREP:<session>:<n>:<gif index>:<loop>' -> (session, n, gif index) or None."""
    p = m.split(":")
    if len(p) == 5 and p[0] == "PREP":
        try:
            return p[1], int(p[2]), int(p[3]) % len(gifs)
        except ValueError:
            return None
    return None


def run_follower():
    global idx, delay
    sess = None          # session id of the leader we are following
    last_n = -1          # newest loop number we have played for that leader
    synced = False       # True while the leader is answering
    alone = 0            # loops played on our own on the current GIF
    heard = []           # a PREP noticed while playing alone (used to re-join quickly)

    def is_new(r):
        # a request is new if the leader restarted (different session) or its counter moved on
        return r[0] != sess or r[1] > last_n

    def listen():
        """Called once per frame while playing alone: has the leader come back?"""
        while True:
            m = poll()
            if m is None:
                return bool(heard)
            r = parse_prep(m)
            if r and is_new(r):
                if not heard:
                    heard.append(r)
                return True

    while True:
        # 1) wait for the leader to tell us what to get ready
        req = heard.pop() if heard else None
        if req is None:
            wait = LEADER_TIMEOUT if synced else ALONE_WAIT
            end = time.monotonic_ns() + int(wait * 1e9)
            while req is None and time.monotonic_ns() < end:
                m = poll()
                if m is None:
                    time.sleep(0.001)
                    continue
                r = parse_prep(m)
                if r and is_new(r):
                    req = r

        if req is None:
            # No leader. Keep the display alive: play one loop, advancing GIFs ourselves.
            if synced:
                print("Follower: leader lost, playing alone")
                synced = False
            delay, aborted = play_one_loop(delay, listen)
            if aborted:
                delay = load_gif(gifs[idx])      # restart this GIF at frame 0, then re-join
            else:
                alone += 1
                if alone >= LOOPS_PER_GIF:
                    alone = 0
                    if len(gifs) > 1:
                        idx = (idx + 1) % len(gifs)
                        delay = load_gif(gifs[idx])
            continue

        cur_sess, newest, midx = req
        if midx != idx:
            idx = midx
            delay = load_gif(gifs[idx])

        # 2) say READY (repeatedly) until the leader sends SYNC (or we give up)
        next_send = 0
        give_up = time.monotonic_ns() + int(SYNC_TIMEOUT * 1e9)
        go = False
        while time.monotonic_ns() < give_up:
            now = time.monotonic_ns()
            if now >= next_send:
                send("READY:%s:%d" % (cur_sess, newest))
                next_send = now + RESEND_NS
            m = poll()
            if m is None:
                time.sleep(0.001)
                continue
            if m == "SYNC:%s:%d" % (cur_sess, newest):
                go = True
                break
            r = parse_prep(m)
            if r and (r[0] != cur_sess or r[1] > newest):   # leader moved on or restarted
                cur_sess, newest, midx = r
                give_up = time.monotonic_ns() + int(SYNC_TIMEOUT * 1e9)
                if midx != idx:
                    idx = midx
                    delay = load_gif(gifs[idx])

        if not go:
            print("Follower: no go signal from leader")
            synced = False
            continue

        if not synced:
            print("Follower: in sync with leader", cur_sess)
        synced = True
        sess, last_n = cur_sess, newest
        alone = 0
        delay, _ = play_one_loop(delay)


# =====================================================================
#                      RUN (and recover from errors)
# =====================================================================
try:
    if ROLE == "leader":
        run_leader()
    else:
        run_follower()
except Exception as e:
    print("ERROR:", type(e).__name__, e)
    if AUTO_RESET:
        print("Restarting in 3 s...")
        time.sleep(3)
        microcontroller.reset()
    raise
