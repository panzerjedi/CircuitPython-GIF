# ESP-NOW link test that shows its results (and the exact error text) ON THE LED PANEL.
# Temporarily save this as code.py on BOTH boards (back up your real code.py first).
# No extra libraries needed.

import os
import time
import board
import displayio
import framebufferio
import rgbmatrix
import terminalio
import wifi
import espnow

# ---------------- Settings ----------------
PANEL_WIDTH = 128
PANEL_HEIGHT = 64
BIT_DEPTH = 3
TEXT_COLOR = 0x00FF00   # green
BROADCAST = b"\xff\xff\xff\xff\xff\xff"

# Red/blue swapped to match this panel's wiring
RGB_PINS = [
    board.MTX_B1, board.MTX_G1, board.MTX_R1,
    board.MTX_B2, board.MTX_G2, board.MTX_R2,
]
# ------------------------------------------

last_error = None
err_count = 0


def note_err(where, e):
    global last_error, err_count
    err_count += 1
    last_error = "%s %s: %s" % (where, type(e).__name__, e)
    print("ERROR:", last_error)


# ---- 1) Start the radio FIRST (before the display grabs timers/memory) ----
radio = None
bpeer = None
try:
    wifi.radio.enabled = True
except Exception as e:
    note_err("enable", e)
try:
    radio = espnow.ESPNow()
    bpeer = espnow.Peer(mac=BROADCAST)
    radio.peers.append(bpeer)
except Exception as e:
    note_err("init", e)

try:
    mac = ":".join("%02x" % b for b in wifi.radio.mac_address)
except Exception:
    mac = "unknown"
if os.getenv("CIRCUITPY_WIFI_SSID"):
    wifi_state = "SET"
elif wifi.radio.connected:
    wifi_state = "CONN"
else:
    wifi_state = "none"
version = os.uname().release

# ---- 2) Now set up the panel ----
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
display = framebufferio.FramebufferDisplay(matrix)   # auto-refresh on

font = terminalio.FONT
fw, fh = font.get_bounding_box()
COLS = PANEL_WIDTH // fw
ROWS = PANEL_HEIGHT // fh

palette = displayio.Palette(2)
palette[0] = 0x000000
palette[1] = TEXT_COLOR
grid = displayio.TileGrid(
    font.bitmap, pixel_shader=palette,
    width=COLS, height=ROWS, tile_width=fw, tile_height=fh,
)
group = displayio.Group()
group.append(grid)
display.root_group = group


def show(row, text):
    text = (text + " " * COLS)[:COLS]
    for col, ch in enumerate(text):
        glyph = font.get_glyph(ord(ch)) or font.get_glyph(ord("?"))
        grid[col, row] = glyph.tile_index


print("MAC", mac, "| wifi:", wifi_state, "| CircuitPython", version)

tx = 0
rx = 0
last_msg = "none yet"
send_failed = radio is None
last_send = time.monotonic()
last_page = -1
dirty = True

while True:
    now = time.monotonic()
    if radio is not None and now - last_send >= 1.0:
        last_send = now
        try:
            radio.send("ping %d" % tx, bpeer)     # send to the broadcast peer explicitly
            tx += 1
            send_failed = False
        except Exception as e:
            send_failed = True
            note_err("send", e)
        dirty = True

    if radio is not None:
        try:
            pkt = radio.read()
        except Exception as e:
            pkt = None
            note_err("read", e)
        if pkt is not None:
            rx += 1
            try:
                last_msg = pkt.msg.decode()
            except Exception:
                last_msg = "?"
            dirty = True

    page = int(now / 3)                     # long error text pages through every 3 s
    if (send_failed or radio is None) and page != last_page:
        last_page = page
        dirty = True

    if dirty:
        dirty = False
        show(0, mac)
        show(1, "TX:%d RX:%d E:%d" % (tx, rx, err_count))
        if (send_failed or radio is None) and last_error:
            chunk = COLS * 2
            pages = (len(last_error) + chunk - 1) // chunk
            seg = last_error[(page % pages) * chunk:(page % pages + 1) * chunk]
            show(2, seg[:COLS])
            show(3, seg[COLS:])
        else:
            show(2, "GOT:" + last_msg)
            show(3, "CP%s WIFI:%s" % (version.split("-")[0], wifi_state))
