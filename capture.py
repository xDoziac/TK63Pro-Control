"""Live data sources for the streaming lighting effects. Windows only, built-in APIs + numpy.

ScreenSampler  - average colour of the primary screen (GDI StretchBlt HALFTONE down to 1x1 pixel,
                 like the official app's 1x1 desktop capture).
AudioLoopback  - captures whatever is playing on the default output device (WASAPI loopback).
Analyser       - reproduces the Web Audio AnalyserNode the official app used (fftSize 2048,
                 Blackman window, smoothing 0.4, -100..-30 dB byte scaling) and its two
                 level formulas: EDM (music3Delt) and Soothe (music2Delt).
"""
import ctypes
import threading
import time
import uuid
from ctypes import wintypes as wt

import numpy as np

# ---------------------------------------------------------------- screen colour
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
try:
    user32.SetProcessDPIAware()
except Exception:
    pass
for fn, res, args in (
    (user32.GetDC, wt.HDC, [wt.HWND]),
    (user32.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
    (gdi32.CreateCompatibleDC, wt.HDC, [wt.HDC]),
    (gdi32.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
    (gdi32.SetStretchBltMode, ctypes.c_int, [wt.HDC, ctypes.c_int]),
    (gdi32.SetBrushOrgEx, wt.BOOL, [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]),
    (gdi32.StretchBlt, wt.BOOL, [wt.HDC] + [ctypes.c_int] * 4 + [wt.HDC] + [ctypes.c_int] * 4 + [wt.DWORD]),
    (gdi32.GetPixel, wt.DWORD, [wt.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
    (gdi32.DeleteDC, wt.BOOL, [wt.HDC]),
):
    fn.restype, fn.argtypes = res, args


class ScreenSampler:
    def __init__(self):
        self.w = user32.GetSystemMetrics(0)
        self.h = user32.GetSystemMetrics(1)

    def sample(self):
        """Return (r, g, b) average of the primary screen."""
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, 1, 1)
        old = gdi32.SelectObject(mem, bmp)
        try:
            gdi32.SetStretchBltMode(mem, 4)  # HALFTONE = area average
            gdi32.SetBrushOrgEx(mem, 0, 0, None)
            gdi32.StretchBlt(mem, 0, 0, 1, 1, screen, 0, 0, self.w, self.h, 0x00CC0020)  # SRCCOPY
            c = gdi32.GetPixel(mem, 0, 0)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


# ---------------------------------------------------------------- WASAPI loopback
ole32 = ctypes.OleDLL("ole32")


class GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


def _guid(s):
    g = GUID()
    ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16)
    return g


CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
SUBTYPE_FLOAT = uuid.UUID("00000003-0000-0010-8000-00AA00389B71").bytes_le


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                ("cbSize", wt.WORD)]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", wt.WORD),
                ("dwChannelMask", wt.DWORD), ("SubFormat", ctypes.c_ubyte * 16)]


def _method(obj, index, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


class AudioLoopback:
    """Background thread keeping the last `size` mono samples of system audio."""

    def __init__(self, size=2048):
        self.size = size
        self.buf = np.zeros(size, dtype=np.float32)
        self.rate = 48000
        self.lock = threading.Lock()
        self.error = None
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def samples(self):
        with self.lock:
            return self.buf.copy()

    def _run(self):
        enum = dev = client = cap = None
        fmt_ptr = ctypes.c_void_p()
        ole32.CoInitializeEx(None, 0)
        try:
            enum = ctypes.c_void_p()
            ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 0x17,
                                   ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))
            dev = ctypes.c_void_p()
            _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                enum, 0, 0, ctypes.byref(dev))  # eRender, eConsole
            client = ctypes.c_void_p()
            _method(dev, 3, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                dev, ctypes.byref(IID_IAudioClient), 0x17, None, ctypes.byref(client))
            _method(client, 8, ctypes.POINTER(ctypes.c_void_p))(client, ctypes.byref(fmt_ptr))  # GetMixFormat
            wfx = ctypes.cast(fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
            channels, bits, align, self.rate = wfx.nChannels, wfx.wBitsPerSample, wfx.nBlockAlign, wfx.nSamplesPerSec
            is_float = wfx.wFormatTag == 3 or (
                wfx.wFormatTag == 0xFFFE and
                bytes(ctypes.cast(fmt_ptr, ctypes.POINTER(WAVEFORMATEXTENSIBLE)).contents.SubFormat) == SUBTYPE_FLOAT)
            _method(client, 3, ctypes.c_int, wt.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)(
                client, 0, 0x00020000, 2_000_000, 0, fmt_ptr, None)  # shared, LOOPBACK, 200 ms
            cap = ctypes.c_void_p()
            _method(client, 14, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                client, ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cap))
            _method(client, 10)(client)  # Start
            get_next = _method(cap, 5, ctypes.POINTER(ctypes.c_uint32))
            get_buf = _method(cap, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p)
            rel_buf = _method(cap, 4, ctypes.c_uint32)
            dtype = np.float32 if is_float else (np.int16 if bits == 16 else np.int32)
            scale = 1.0 if is_float else float(2 ** (bits - 1))
            while self.running:
                time.sleep(0.008)
                n = ctypes.c_uint32()
                get_next(cap, ctypes.byref(n))
                while n.value and self.running:
                    data, frames, flags = ctypes.c_void_p(), ctypes.c_uint32(), wt.DWORD()
                    get_buf(cap, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
                    count = frames.value
                    if flags.value & 2 or not data.value:  # AUDCLNT_BUFFERFLAGS_SILENT
                        mono = np.zeros(count, dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data.value, count * align)
                        arr = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
                        mono = arr.reshape(-1, channels).mean(axis=1) if channels > 1 else arr
                    rel_buf(cap, count)
                    with self.lock:
                        if len(mono) >= self.size:
                            self.buf[:] = mono[-self.size:]
                        elif len(mono):
                            self.buf = np.concatenate([self.buf[len(mono):], mono])
                    get_next(cap, ctypes.byref(n))
            _method(client, 11)(client)  # Stop
        except Exception as e:
            self.error = f"Couldn't capture system audio: {e}"
        finally:
            if fmt_ptr.value:
                ole32.CoTaskMemFree(fmt_ptr)
            for o in (cap, client, dev, enum):
                _release(o)
            ole32.CoUninitialize()
            self.running = False


# ---------------------------------------------------------------- analysis (Web Audio AnalyserNode clone)
class Analyser:
    def __init__(self, fft_size=2048, smoothing=0.4, min_db=-100.0, max_db=-30.0):
        self.n = fft_size
        k = np.arange(fft_size)
        self.window = 0.42 - 0.5 * np.cos(2 * np.pi * k / fft_size) + 0.08 * np.cos(4 * np.pi * k / fft_size)
        self.smoothing = smoothing
        self.min_db, self.max_db = min_db, max_db
        self.prev = np.zeros(fft_size // 2)

    def update(self, samples):
        x = samples[-self.n:] * self.window
        mag = np.abs(np.fft.rfft(x))[: self.n // 2] / self.n
        self.prev = self.smoothing * self.prev + (1 - self.smoothing) * mag
        with np.errstate(divide="ignore"):
            self.float_db = 20 * np.log10(self.prev)
        b = 255.0 / (self.max_db - self.min_db) * (self.float_db - self.min_db)
        self.byte = np.clip(np.nan_to_num(b, neginf=0.0), 0, 255).astype(np.uint8)

    def edm_levels(self):
        """music3Delt: 32 levels 0..6 from byte bins 27..58, normalised against bins 20..39."""
        e = self.byte[:100].astype(np.int32)
        t = e - int(e[20:40].min())
        peak = int(t.max())
        if peak <= 0:
            return [0] * 32
        lv = np.clip(np.round(t / (peak / 6.0)), 0, 6).astype(int)
        return lv[27:59].tolist()

    def soothe_levels(self):
        """music2Delt: 13 zeros + float bins 0..29 scaled so -28 dB maps to 6 (sent from offset 14)."""
        t = np.concatenate([np.zeros(13), self.float_db[:30]])
        if not np.all(np.isfinite(t)):
            return [0] * 43
        lo = t.min()
        r = (-28.0 - lo) / 6.0
        if r == 0:
            return [0] * 43
        v = np.clip((t - lo) / r, 0, 15)
        return v.astype(int).tolist()
