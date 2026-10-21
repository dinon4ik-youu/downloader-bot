"""Поднять связь с iPhone для автоматизации Edits (после перезагрузки, переподключения кабеля, обрыва).

  python tools/wda/start.py

1. ждёт iPhone в usbmux (если нет — переподключить кабель, разблокировать);
2. туннель: если не отвечает http://127.0.0.1:49151 — запускает `pymobiledevice3 remote tunneld` от администратора (UAC);
3. подключает инструменты разработчика (auto-mount);
4. запускает WebDriverAgent и проброс порта 8100 в фоне, ждёт «ready».
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import place  # noqa: E402

NO_WINDOW = 0x08000000
PMD = [sys.executable, "-m", "pymobiledevice3"]  # не зависит от PATH (в наборе — свой Python)
DEVICE = place.DATA / "device.json"  # UDID телефона и bundle id WDA (у каждого свой: в нём Team ID подписи)


def pmd(*args):
    return subprocess.run([*PMD, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW).stdout


def device():
    """UDID первого iPhone по кабелю и bundle id WDA — сами (21.10.2026, для набора); запомнить в data/wda/device.json."""
    try:
        d = json.loads(DEVICE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    try:
        devs = json.loads(pmd("usbmux", "list") or "[]")
    except ValueError:
        devs = []
    ids = [x.get("Identifier") or x.get("UniqueDeviceID") for x in devs]
    if not ids:
        return None, None
    if d.get("udid") not in ids:
        d = {"udid": ids[0]}
    if not d.get("wda"):
        try:
            apps = json.loads(pmd("apps", "list", "--type", "User") or "{}")
        except ValueError:
            apps = {}
        wda = [b for b in apps if "WebDriverAgentRunner" in b]
        if not wda:
            sys.exit("на iPhone нет WebDriverAgent — поставь его через Sideloadly (INSTALL-AI.md)")
        d["wda"] = wda[0]
    DEVICE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    return d["udid"], d["wda"]


def get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def ready():
    st = get("http://127.0.0.1:8100/status")
    return bool(st and st.get("value", {}).get("ready"))


def main():
    if ready():
        print("WebDriverAgent уже работает")
        return
    print("жду iPhone по кабелю…")
    for i in range(60):
        UDID, WDA = device()
        if UDID:
            break
        if i == 3:
            print("  не вижу — переподключи кабель и разблокируй телефон")
        time.sleep(3)
    else:
        sys.exit("iPhone так и не появился")
    if UDID not in json.dumps(get("http://127.0.0.1:49151/") or {}):
        if get("http://127.0.0.1:49151/") is None:
            # задача «WDA tunnel» (tunnel-task.ps1, 19.10.2026) — туннель от администратора без окна UAC
            if subprocess.run(["schtasks", "/run", "/tn", "WDA tunnel"], capture_output=True).returncode == 0:
                print("запускаю туннель (задача «WDA tunnel»)")
            else:
                print("запускаю туннель (UAC — «Да», окно не закрывать)")
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command',"
                                f"\"& '{sys.executable}' -m pymobiledevice3 remote tunneld\""])
        for _ in range(40):
            if UDID in json.dumps(get("http://127.0.0.1:49151/") or {}):
                break
            time.sleep(2)
        else:
            sys.exit("туннель к телефону не поднялся")
    print("туннель есть; подключаю инструменты разработчика")
    pmd("mounter", "auto-mount", "--tunnel", UDID)
    print("запускаю WebDriverAgent")
    subprocess.Popen([*PMD, "developer", "dvt", "xcuitest", WDA, "--tunnel", UDID],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if subprocess.run(["powershell", "-NoProfile", "-Command",
                       "Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue"],
                      capture_output=True, text=True).stdout.strip() == "":
        subprocess.Popen([*PMD, "usbmux", "forward", "8100", "8100"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    for _ in range(40):
        if ready():
            print("WebDriverAgent готов")
            return
        time.sleep(2)
    sys.exit("WebDriverAgent не ответил — телефон разблокирован? подпись WDA не истекла (Sideloadly, раз в 7 дней)?")


if __name__ == "__main__":
    main()
