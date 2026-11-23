# TK63Pro-Control

An unofficial, open-source replacement for the **Tezarre Driver**, for the **Tezarre TK-63 Pro** mechanical keyboard.

Tezarre has disappeared and its software is hard to find. This app lets you keep controlling your keyboard's
lighting and settings. It's small, it has no ads or accounts, and it never phones home.

> Not affiliated with or endorsed by Tezarre. Use at your own risk.

## Download and run

**Easiest:** download `TK63Pro-Control.exe` from the [Releases](../../releases) page and double-click it.
A window opens. Close the window and the app exits about 30 seconds later.

- Windows may show a SmartScreen warning because the app isn't code-signed. Click **More info → Run anyway**.
  Some antivirus tools also flag freshly built PyInstaller apps. If you'd rather not trust a download, run it from source instead (below).
- **Close the official Tezarre Driver first, including its tray icon.** Two programs can't control the keyboard at once.

**From source:** install [Python 3.10+](https://www.python.org/downloads/), then in this folder:

```
pip install -r requirements.txt
pythonw app.py
```

Or double-click `Start TK63 Pro Control.bat`.

## Supported keyboards

| Keyboard | Status |
|---|---|
| Tezarre **TK-63 Pro**, USB cable | ✅ Tested (firmware 770) |
| Tezarre **TK-63 Pro**, 2.4 GHz dongle | ✅ Tested, including battery level |
| TK-63 Pro over Bluetooth | ❌ Not supported. Use the cable or dongle. |
| Tezarre TK-63 (non-Pro), TK-61 and others | ❓ Untested. The official driver treats them as different models. The app warns you if your keyboard reports a different model. Please open an issue with the output of `python probe.py`. |

## Features

- **19 lighting effects** using the official names (Always On, Dynamic breathing, Spectrum cycle, Drift, Waves ripple,
  Starry, The steady stream, Shadowing, Peaks rising one after another, Sine Wave, Caispring Surging, Flowers Blooming,
  One Stone Two Birds, Peak Turn, Inclined rain, Snow, Meteor, Through the snow non-trace), each with:
  colour or rainbow, brightness, speed, and direction or pattern where the keyboard supports it
- **Live effects** (these run while the app is open):
  - **Music Follow (EDM)** and **Music Follow (Soothe)** react to whatever is playing on your PC, with a smoothness slider
  - **Light Shadow** makes the keyboard follow the average colour of your screen
- **Key sensitivity** (1 = least, 10 = most sensitive)
- **Sleep timers**: lights-off and sleep delays, separately for 2.4G and Bluetooth
- **Polling rate**: 125 / 250 / 500 / 1000 Hz
- **Presets** you can save and re-apply
- An animated **preview** of each effect
- A **watchdog log** and notes box for troubleshooting

Settings are stored **on the keyboard**, so they stay after you close the app or move to another PC.

### Deliberately not included
Firmware updates, factory reset, key remapping and macros. These are the features that can leave a keyboard
unusable if anything goes wrong, so this app doesn't touch them.

## Good to know

- **Changes take a second or two.** After each change, the keyboard saves to its memory and ignores USB for a moment.
  If it's sent more commands during that window, its settings channel jams until it's left alone or replugged.
  The app queues your changes and combines quick ones, such as dragging a slider. Typing is never affected.
- If the app says **"The keyboard stopped answering"**, unplug the keyboard (or dongle) and plug it back in.
- The **live effects** need the app to stay open, just like with the official driver.

## How it works

The TK-63 Pro uses a generic "RongYuan" keyboard controller shared by many brands. In the official driver its model ID is 347.
The app talks to it with 64-byte HID feature reports on the keyboard's own USB interface, using only built-in Windows
libraries through `ctypes`. Over the 2.4G dongle there's an extra relay handshake (`0xFB`/`0xFC`/`0xFE`/`0xFD`).
`tk63pro.py` documents the command bytes, checksums and timing rules.

| File | What it is |
|---|---|
| `app.py` | The app: background worker that paces all USB traffic, plus the local window server (127.0.0.1 only) |
| `tk63pro.py` | Keyboard protocol |
| `hid_win.py` | Minimal Windows HID access (`hid.dll` / `setupapi.dll`) |
| `capture.py` | Screen colour and system-audio capture, plus the music analysis for the live effects |
| `ui/index.html` | The window |
| `probe.py` | Read-only diagnostic that prints what your keyboard reports |

## Contributing

Bug reports and pull requests are welcome, especially from owners of other Tezarre models. For a new model, include:
- the output of `python probe.py` (it's read-only and changes nothing)
- which connection you used (cable or dongle)

## License

MIT. See [LICENSE](LICENSE).
