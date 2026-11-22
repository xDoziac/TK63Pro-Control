"""Tezarre TK-63 Pro protocol (RongYuan "YC200/YC300" keyboard family).

Reconstructed from the official Tezarre Driver 1.1.31 (classes USBDevice -> YC200Common -> YC300_TK63PRO).
All commands are 64-byte HID feature reports (65 with report ID 0) on the
usage-page 1 / usage 6 (keyboard) collection whose feature report length is 65.

Timing rules found by testing on a real TK-63 Pro (firmware 770, USB cable):
  * After a settings WRITE the keyboard is busy for a while. Any USB request in that window
    (a read 0.3 s later, or a second write 150 ms later) jams its settings channel until it is
    left alone for a long time or unplugged. Writes 2 s apart work; reads 7 s+ after a write work.
  * So: never read right after a write, and space writes out. `QUIET_AFTER_WRITE` enforces that
    in the app's worker thread - this module only does single, plain transfers.

Deliberately NOT implemented: firmware/bootloader (0x7F), factory reset (0x02),
key remapping and macros - these can leave a keyboard unusable if done wrong.
"""
import time
from hid_win import enumerate_devices, HidDevice

TK63PRO_ID = 347
SUPPORTED = {  # (vid, pid): description
    (0x25A7, 0x2420): "2.4G dongle",
    (0x0461, 0x4003): "USB cable",
}
QUIET_AFTER_WRITE = 3.0   # seconds of silence after any write
QUIET_BEFORE_READ = 8.0   # seconds since last write before a read is allowed

CMD_GET_REV = 0x80
CMD_GET_INFO = 0x8F          # "who am I": int32 LE device id at bytes 1..4
CMD_SET_REPORT, CMD_GET_REPORT = 0x04, 0x84
CMD_SET_LEDPARAM, CMD_GET_LEDPARAM = 0x07, 0x87
CMD_SET_DEBOUNCE, CMD_GET_DEBOUNCE = 0x11, 0x91   # "key sensitivity" 1 (fast) .. 10 (slow)
CMD_SET_SLEEP, CMD_GET_SLEEP = 0x12, 0x92         # light-off / sleep timers, seconds
DEBOUNCE_RANGE = (1, 10)
LIGHT_OFF_MIN = (1, 60)    # minutes
SLEEP_MIN = (10, 60)       # minutes
D24_STATUS, D24_READING, D24_SET_LEN, D24_CHECK = 0xFB, 0xFC, 0xFE, 0xFD  # 2.4G dongle link layer

MAXSPEED = 4
DAZZLE, NORMAL = 8, 7          # low nibble of byte 4: rainbow vs single colour
WHITE_FIX = 0xFAFFFA           # the official app sends this instead of pure white

# id, name (official Tezarre name), mode byte, colour?, speed?, options [(label, value)]
# Option values go in the high nibble of byte 4. Only the options the official app offers - extra ones
# (Meteor/Snow direction, Ripple/Neon/Shadowing variants) were tested on a real TK-63 Pro and ignored.
EFFECTS = [
    ("off", "Off", 0, False, False, None),
    ("static", "Always On", 1, True, False, None),
    ("breathing", "Dynamic breathing", 2, True, True, None),
    ("neon", "Spectrum cycle", 3, False, True, None),
    ("wave", "Drift", 4, True, True, [("Right", 0), ("Left", 1), ("Down", 2), ("Up", 3)]),
    ("ripple", "Waves ripple", 5, True, True, None),
    ("raindrop", "Starry", 6, True, True, None),
    ("snake", "The steady stream", 7, True, True, [("Z pattern", 0), ("Spiral (Return)", 1)]),
    ("reactive", "Shadowing", 8, True, True, None),
    ("converge", "Peaks rising one after another", 9, True, True, None),
    ("sine", "Sine Wave", 10, True, True, None),
    ("kaleido", "Caispring Surging", 11, True, True, [("Outward", 0), ("Inward", 1)]),
    ("linewave", "Flowers Blooming", 12, True, True, [("Right", 0), ("Left", 1)]),
    ("laser", "One Stone Two Birds", 14, True, True, None),
    ("circle", "Peak Turn", 15, True, True, [("Anti-clockwise", 0), ("Clockwise", 1)]),
    ("dazzling", "Inclined rain", 16, True, True, None),
    ("raindown", "Snow", 17, True, True, None),
    ("meteor", "Meteor", 18, True, True, None),
    ("reactoff", "Through the snow non-trace", 19, True, True, None),
    # Live effects: the PC streams data while the app is open (official: LightMusicFollow3 / LightMusicFollow2 / LightScreenColor)
    ("music_edm", "Music Follow (EDM)", 20, True, False, [("Upright", 0), ("Separate", 1), ("Intersect", 2)]),
    ("music_soothe", "Music Follow (Soothe)", 22, True, False, [("Upright", 0), ("Separate", 1), ("Intersect", 2)]),
    ("screen", "Light Shadow (screen colour)", 21, False, False, None),
]
EFFECT_BY_MODE = {e[2]: e for e in EFFECTS}
EXPERIMENTAL_OPTIONS = set()
DESCRIPTIONS = {
    0: "All lights off.",
    1: "Every key lit in one colour (or a fixed rainbow).",
    2: "The whole keyboard breathes in one colour; in rainbow mode the colour changes each breath.",
    3: "The whole keyboard shows one colour that slowly morphs into the next, dimming slightly in between.",
    4: "Bands of colour flow across the keyboard in the chosen direction.",
    5: "Pressing a key sends a ripple outwards across the keyboard, like a stone dropped in water.",
    6: "Random keys twinkle like stars.",
    7: "Z pattern: a snake runs Esc to Backspace, down to \\, back to Tab, and so on. "
       "Spiral: it circles the outer edge, spirals inward, then plays it all in reverse.",
    8: "The keys you press light up and fade out.",
    9: "Two lights start at Esc and the left arrow, meet in the middle, fuse, then sweep out to both edges.",
    10: "A sine wave rolls across the keyboard.",
    11: "Surging colour patterns spreading out from (or into) the centre.",
    12: "Every key lit at once, each in its own colour, with the colours slowly shifting.",
    14: "Press a key and light shoots out both ways along that row to the edges.",
    15: "A circular wave turning around the keyboard.",
    16: "Rain falling at an angle.",
    17: "Snow / rain falling straight down the columns.",
    18: "Meteors streaking horizontally across the rows.",
    19: "Every key is lit; the keys you press go dark and fade back.",
    20: "Bars bounce to whatever is playing on your PC - punchy, for beat-heavy music.",
    22: "A gentle, smoother music visualiser for calmer music.",
    21: "The keyboard follows the average colour of your screen.",
}
LIVE_MODES = {20: "music_edm", 22: "music_soothe", 21: "screen"}
CMD_STREAM_AUDIO = 0x0D   # FEA_CMD_SET_AUDIO
CMD_STREAM_SCREEN = 0x0E  # FEA_CMD_SET_WINDOS


def _cksum7(b):   # getBufSe
    b[7] = 0xFF - (sum(b[0:7]) & 0xFF)
    return b


def _cksum8(b):   # getBufEi
    b[8] = 0xFF - (sum(b[0:8]) & 0xFF)
    return b


def find_keyboard():
    """Return (device-info dict, connection description) or (None, None)."""
    for d in enumerate_devices():
        key = (d["vid"], d["pid"])
        if key in SUPPORTED and d["usage_page"] == 1 and d["usage"] == 6 and d["feature_len"] == 65:
            return d, SUPPORTED[key]
    return None, None


def lighting_packet(mode, brightness=4, speed=2, color="#00C8FF", rainbow=False, option=0):
    if mode not in EFFECT_BY_MODE:
        raise ValueError("unknown mode")
    b = bytearray(64)
    b[0] = CMD_SET_LEDPARAM
    b[1] = mode
    b[2] = MAXSPEED - max(0, min(MAXSPEED, int(speed)))
    b[3] = max(0, min(4, int(brightness)))
    if mode == 0:
        b[4] = 7
    elif mode == 3:          # Spectrum cycle has no colour; high nibble = Default/Random
        b[4] = ((int(option) & 0x0F) << 4) | 7
    elif mode in (20, 22):   # music: low nibble is 0 for rainbow, 4 for single colour
        b[4] = ((int(option) & 0x0F) << 4) | (0 if rainbow else 4)
    elif mode == 21:         # screen colour: fixed full brightness, default flags
        b[2], b[3], b[4] = MAXSPEED, 4, 7
    else:
        b[4] = ((int(option) & 0x0F) << 4) | (DAZZLE if rainbow else NORMAL)
    rgb = int(str(color).lstrip("#"), 16)
    if rgb == 0xFFFFFF:
        rgb = WHITE_FIX
    b[5], b[6], b[7] = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
    return _cksum8(b)


def parse_lighting(r):
    mode, speed, bright, flags = r[1], MAXSPEED - r[2], r[3], r[4]
    rgb = (r[5] << 16) | (r[6] << 8) | r[7]
    if rgb == WHITE_FIX:
        rgb = 0xFFFFFF
    rainbow = (flags & 0x0F) == 0 if mode in (20, 22) else (flags & 0x0F) == DAZZLE
    return dict(mode=mode, speed=max(0, min(4, speed)), brightness=min(4, bright),
                option=flags >> 4, rainbow=rainbow, color=f"#{rgb:06X}")


def screen_packet(r, g, b):
    """setScreen: [0x0E, R, G, B, A, 0, 0, checksum] (official sends the 1x1 RGBA pixel)."""
    p = bytearray(64)
    p[0], p[1], p[2], p[3], p[4] = CMD_STREAM_SCREEN, r & 255, g & 255, b & 255, 255
    return _cksum7(p)


def audio_packet(levels, offset, is24):
    """setMusicFollow: 32 levels starting at `offset` (slid back until >= 5 are non-zero).
    Wired: 8-byte header [0x0D, 0.., cksum] + 32 bytes. 2.4G: two levels packed per byte after 0x0D."""
    i = offset
    e = list(levels[i:i + 32])
    while sum(1 for v in e if v) < 5 and i > 0:
        i -= 1
        e = list(levels[i:i + 32])
    e = [max(0, min(255, int(v))) for v in e]
    if is24:
        head = [CMD_STREAM_AUDIO]
    else:
        head = [CMD_STREAM_AUDIO, 0, 0, 0, 0, 0, 0, 0xFF - CMD_STREAM_AUDIO]
    p = bytearray((head + e + [0] * 64)[:64])
    if is24:
        for a in range((len(e) + 1) // 2):
            hi = e[2 * a + 1] if 2 * a + 1 < len(e) else 0
            p[a + 1] = (e[2 * a] | (hi << 4)) & 0xFF
    return p, len(e)


class TK63Pro:
    """Single plain transfers. Callers must respect QUIET_AFTER_WRITE / QUIET_BEFORE_READ."""

    def __init__(self, info):
        self.dev = HidDevice(info["path"], info["feature_len"])
        self.is24 = False

    # ---- 2.4G dongle link layer (talks to the dongle, not the keyboard) ----
    def _raw(self, cmd, arg=None):
        b = bytearray(64)
        b[0] = cmd
        if arg is not None:
            b[1] = arg
        return self.dev.send_feature(b)

    def status24(self):
        self._raw(D24_STATUS)
        time.sleep(0.01)
        r = self.dev.get_feature()
        if r is None:
            return None
        return dict(ready_read=bool(r[0]), battery=r[1], online=not bool(r[2]), write_done=bool(r[3]))

    def _wait24(self, key, tries=50):
        for _ in range(tries):
            time.sleep(0.02)
            s = self.status24()
            if s and s[key]:
                return True
        return False

    def check24(self):
        """Only meaningful on the dongle; over the cable we skip it entirely."""
        self._raw(D24_CHECK)
        time.sleep(0.01)
        r = self.dev.get_feature()
        self.is24 = bool(r and r[0] == 0xAA and r[1] == 0x55)
        return self.is24

    def online(self):
        if not self.is24:
            return True
        s = self.status24()
        return bool(s and s["online"])

    # ---- transfers ----
    def write(self, buf):
        self.last_packet = bytes(buf[:16])  # for the watchdog log
        if self.is24:
            if not self.online():
                return False
            self._wait24("write_done")
        ok = self.dev.send_feature(buf)
        if ok and self.is24:
            self._wait24("write_done")
        return ok

    def query(self, cmd, arg=None):
        b = bytearray(64)
        b[0] = cmd
        if arg is not None:
            b[1] = arg
        if self.is24:
            self._raw(D24_SET_LEN, 64)
            time.sleep(0.01)
        if not self.write(_cksum7(b)):
            return None
        if self.is24:
            if not self._wait24("ready_read"):
                return None
            self._raw(D24_READING)
            time.sleep(0.01)
        time.sleep(0.005)
        r = self.dev.get_feature()
        if r is None or (not self.is24 and r[0] != cmd):
            return None
        return r

    # ---- high level ----
    def who_am_i(self):
        r = self.query(CMD_GET_INFO)
        return None if r is None else int.from_bytes(r[1:5], "little", signed=True) & 0x7FFFFFFF

    def firmware(self):
        r = self.query(CMD_GET_REV)
        return None if r is None else int.from_bytes(r[1:3], "little")

    def get_report_rate(self):
        r = self.query(CMD_GET_REPORT, 0)
        return {1: 1000, 2: 500, 4: 250, 8: 125}.get(r[2]) if r else None

    def get_lighting(self):
        r = self.query(CMD_GET_LEDPARAM)
        return None if r is None else parse_lighting(r)

    def set_lighting(self, **kw):
        return self.write(lighting_packet(**kw))

    def get_debounce(self):
        r = self.query(CMD_GET_DEBOUNCE)
        return None if r is None else r[2]

    def set_debounce(self, value):
        b = bytearray(64)
        b[0], b[2] = CMD_SET_DEBOUNCE, max(DEBOUNCE_RANGE[0], min(DEBOUNCE_RANGE[1], int(value)))
        return self.write(_cksum7(b))

    def get_sleep(self):
        """Minutes: light_off_bt, light_off_24, sleep_bt, sleep_24 (reply carries seconds at bytes 1..8)."""
        r = self.query(CMD_GET_SLEEP)
        if r is None:
            return None
        sec = [r[1] | r[2] << 8, r[3] | r[4] << 8, r[5] | r[6] << 8, r[7] | r[8] << 8]
        return dict(light_off_bt=sec[0] // 60, light_off_24=sec[1] // 60,
                    sleep_bt=sec[2] // 60, sleep_24=sec[3] // 60)

    def set_sleep(self, light_off_bt, light_off_24, sleep_bt, sleep_24):
        def clamp(v, rng):
            return max(rng[0], min(rng[1], int(v))) * 60
        vals = [clamp(light_off_bt, LIGHT_OFF_MIN), clamp(light_off_24, LIGHT_OFF_MIN),
                clamp(sleep_bt, SLEEP_MIN), clamp(sleep_24, SLEEP_MIN)]
        b = bytearray(64)
        b[0] = CMD_SET_SLEEP
        _cksum7(b)  # the official app computes the checksum before filling in the data...
        for i, v in enumerate(vals):  # ...and writes the values at bytes 8..15 (unlike the reply's 1..8)
            b[8 + 2 * i], b[9 + 2 * i] = v & 0xFF, v >> 8
        return self.write(b)

    # ---- live streaming (no 2.4G write-finish waits, like the official app) ----
    def stream_screen(self, r, g, b):
        if self.is24:
            self._raw(D24_SET_LEN, 8)
        return self.dev.send_feature(screen_packet(r, g, b))

    def stream_audio(self, levels, offset):
        p, n = audio_packet(levels, offset, self.is24)
        if self.is24:
            self._raw(D24_SET_LEN, n // 2 + 1)
        return self.dev.send_feature(p)

    def set_report_rate(self, hz):
        b = bytearray(64)
        b[0], b[1], b[2] = CMD_SET_REPORT, 0, {1000: 1, 500: 2, 250: 4, 125: 8}[int(hz)]
        return self.write(_cksum7(b))

    def close(self):
        self.dev.close()
