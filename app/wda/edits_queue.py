"""Очередь прогонов Edits на айфон (20.10.2026): одна серия за раз на телефон.
Общая для edits_iphone.py (встаёт в очередь и ждёт) и бота-качалки (место в очереди, кнопки «⬆️ Выше / ⬇️ Ниже»).
queue.json — [{pid, created, series, since, started?}] в порядке очереди; мёртвые процессы вычищаются при каждом чтении.
Начатый прогон («started») всегда впереди — его не обгоняют."""
import json
import time
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import place  # noqa: E402

QUEUE = place.DATA / "queue.json"
LOCK = place.DATA / "queue.lock"


def alive(e):
    import psutil
    try:
        return abs(psutil.Process(e["pid"]).create_time() - e["created"]) < 1
    except psutil.Error:
        return False


def edit(fn=None):
    """Прочитать очередь под замком (msvcrt) и, если задано, поменять её: fn(список) -> список."""
    import msvcrt
    with open(LOCK, "a+") as lk:
        while True:
            try:
                msvcrt.locking(lk.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.2)
        try:
            try:
                q = json.loads(QUEUE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                q = []
            q = [e for e in q if alive(e)]
            if fn:
                q = fn(q)
            QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
            return q
        finally:
            lk.seek(0)
            msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)


def position(pid):
    """→ (место среди ждущих, начиная с 1; 0 — уже идёт на айфоне; None — нет в очереди), всего ждущих."""
    q = edit()
    waiting = [e for e in q if not e.get("started")]
    for e in q:
        if e["pid"] == pid:
            return (0 if e.get("started") else waiting.index(e) + 1), len(waiting)
    return None, len(waiting)


def move(pid, step):
    """Сдвинуть ждущий прогон на step мест (−1 — выше, +1 — ниже), не выше начатых. → True, если сдвинул."""
    moved = []

    def fn(q):
        i = next((k for k, e in enumerate(q) if e["pid"] == pid), None)
        if i is None or q[i].get("started"):
            return q
        first = sum(1 for e in q if e.get("started"))
        j = min(max(i + step, first), len(q) - 1)
        if j != i:
            q.insert(j, q.pop(i))
            moved.append(True)
        return q
    edit(fn)
    return bool(moved)
