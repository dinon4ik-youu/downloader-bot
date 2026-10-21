"""Разведка экранов: дамп непустых label/name текущего экрана + снимок.
  python tools/wda/explore.py <имя>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wda  # noqa: E402

name = sys.argv[1]
xml = wda.call("GET", "/source", timeout=120)["value"]
(wda.SHOTS / f"{name}.xml").write_text(xml, encoding="utf-8")
wda.shot(name)
seen = set()
for m in re.finditer(r'type="XCUIElementType(\w+)" name="([^"]*)" label="([^"]*)"[^>]*?x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', xml):
    t, n, l, x, y, w, h = m.groups()
    if (n or l) and (t, n, l) not in seen and t not in ("ScrollView",):
        seen.add((t, n, l))
        print(f"{t:12} {x:>4},{y:<4} {w}x{h}  name={n!r}  label={l!r}")
