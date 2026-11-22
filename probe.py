"""Read-only diagnostic: finds the keyboard and asks it questions. Changes nothing.
Close TK-63 Pro Control and the official Tezarre Driver before running it."""
import time
from tk63pro import find_keyboard, TK63Pro, TK63PRO_ID

info, how = find_keyboard()
if not info:
    raise SystemExit("Keyboard not found. Plug in the USB cable or the 2.4G dongle.")
print("Connection:", how)
kb = TK63Pro(info)
try:
    if how == "2.4G dongle":
        kb.check24()
        s = kb.status24()
        print("Battery:", s and s["battery"])
        if not kb.online():
            print("Keyboard is asleep - press any key on it (waiting up to 60 s)...", flush=True)
            end = time.time() + 60
            while time.time() < end and not kb.online():
                time.sleep(0.25)
            if not kb.online():
                raise SystemExit("Keyboard never woke up. Is it switched on and in 2.4G mode?")
    dev_id = kb.who_am_i()
    print("Model id:", dev_id, "(TK-63 Pro)" if dev_id == TK63PRO_ID else "(not answering, or not a TK-63 Pro)")
    print("Firmware:", kb.firmware())
    print("Report rate:", kb.get_report_rate())
    print("Lighting:", kb.get_lighting())
finally:
    kb.close()
