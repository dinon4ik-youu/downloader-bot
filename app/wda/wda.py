"""Ручное управление iPhone через WebDriverAgent (порт 8100 проброшен usbmux forward).

  python tools/wda/wda.py shot [имя]          # снимок экрана -> tools/wda/shots/<имя>.png
  python tools/wda/wda.py tap X Y [имя]       # нажать в точку (пиксели снимка, у iPhone 11 828x1792), потом снимок
  python tools/wda/wda.py hold X Y [имя]      # долгое нажатие, потом снимок
  python tools/wda/wda.py tree [имя]          # дерево элементов -> tools/wda/shots/<имя>.xml
  python tools/wda/wda.py open BUNDLE         # открыть приложение
"""
import base64
import json
import struct
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8100"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import place  # noqa: E402

SHOTS = place.DATA / "shots"


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def session():
    st = call("GET", "/status")
    sid = st.get("sessionId")
    if not sid:
        sid = call("POST", "/session", {"capabilities": {"alwaysMatch": {}}})["sessionId"]
    return sid


def shot(name="last"):
    SHOTS.mkdir(exist_ok=True)
    p = SHOTS / f"{name}.png"
    p.write_bytes(base64.b64decode(call("GET", "/screenshot")["value"]))
    print(p)
    return p


def window(sid):
    """Размер экрана в точках (в них идут нажатия): iPhone 11 — 414x896, у других моделей свой."""
    s = call("GET", f"/session/{sid}/window/size")["value"]
    return s["width"], s["height"]


def scale(sid):
    """Пикселей снимка на точку: 2 у iPhone 11 / XR / SE, 3 у большинства остальных."""
    png = base64.b64decode(call("GET", "/screenshot")["value"])
    return struct.unpack(">I", png[16:20])[0] / window(sid)[0]


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "shot":
        shot(*args[:1])
    elif cmd == "tap":
        sid = session()
        k = scale(sid)
        x, y = int(args[0]) / k, int(args[1]) / k
        call("POST", f"/session/{sid}/wda/tap", {"x": x, "y": y})
        time.sleep(2)
        shot(*args[2:3])
    elif cmd == "hold":
        sid = session()
        k = scale(sid)
        x, y = int(args[0]) / k, int(args[1]) / k
        call("POST", f"/session/{sid}/wda/touchAndHold", {"x": x, "y": y, "duration": 1.0})
        time.sleep(1.5)
        shot(*args[2:3])
    elif cmd == "tree":
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"{args[0] if args else 'tree'}.xml"
        p.write_text(call("GET", "/source", timeout=120)["value"], encoding="utf-8")
        print(p)
    elif cmd == "open":
        call("POST", f"/session/{session()}/wda/apps/launch", {"bundleId": args[0]})
        time.sleep(3)
        shot()


if __name__ == "__main__":
    main()
