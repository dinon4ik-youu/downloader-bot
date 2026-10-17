"""Клипы канала Kick по стримам — склеенные в моменты (панчи).

  python tools/kick_clips.py                 — список стримов канала (номер, дата, длина, название)
  python tools/kick_clips.py https://kick.com/kalatay3/videos/<uuid>  — моменты стрима по ссылке на запись
                                             (ссылка на сам канал = последний стрим)
  python tools/kick_clips.py 1               — то же по номеру из списка (1 = последний)
  python tools/kick_clips.py 2026-10-15      — то же по дате начала стрима (UTC)
  python tools/kick_clips.py 132488180       — то же по id стрима
  --channel <slug>  — другой канал (по умолчанию kalatay3)
  --time            — моменты по ходу стрима, а не по числу клипов
  --min-clips N     — только моменты, которые заклипали N+ раз (по умолчанию 2)
  --raw             — без склейки: все клипы списком по просмотрам
  --fresh           — не брать сохранённое, собрать заново

Как склеивается (проверено Димой на стриме 15.10.2026):
  - у клипа в плейлисте m3u8 есть настоящее время эфира (PROGRAM-DATE-TIME) — по нему отрезок клипа;
  - клипы — один момент, если общий кусок >= 50% более короткого из двух;
  - начало панча — первая секунда, которую захватили >= 95% клипов момента (точность ~2 с);
  - потом +2 с: по проверке Димы панч на ~2 с позже;
  - «короткий» — клип до 60 с, где панч на 1–5-й секунде, из таких — с максимумом просмотров
    (готовый клип: внимание надо взять в первые 3–4 с; Дима выбрал так 17.10.2026);
  - «длинный» — клип, где панч ближе всего к середине и запас до/после максимальный
    (материал для своей нарезки: предыстория до панча тоже попадает).
Сбор стрима на ~2500 клипов — ~5 мин; результат сохраняется в tools/kick_cache/.
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
CACHE = Path(__file__).parent / "kick_cache"
SHORT_MAX = 60     # «короткий» клип — не длиннее, с
OVERLAP = 0.5      # доля более короткого клипа, чтобы считать один момент
PUNCH = 0.95       # доля клипов момента, захвативших секунду, = начало панча
SHIFT = 2          # по проверке Димы панч на ~2 с позже найденной секунды
LEAD = (1, 5)      # у «короткого» панч на 1–5-й секунде: внимание надо взять в первые 3–4 с


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    for attempt in range(5):  # Kick иногда подвисает на запросе — повторяем
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8")
        except (TimeoutError, OSError):
            if attempt == 4:
                raise
            time.sleep(2)


def get(url):
    return json.loads(fetch(url))


def streams(slug):
    return [{
        "id": str(v["id"]),
        "title": (v.get("session_title") or "").strip(),
        "start": v["start_time"],  # UTC, "2026-10-16 17:04:10"
        "uuid": (v.get("video") or {}).get("uuid", ""),
        "hours": round((v.get("duration") or 0) / 3_600_000, 1),
    } for v in get(f"https://kick.com/api/v2/channels/{slug}/videos")]


def find_stream(st, key):
    """Стрим по id, старому uuid записи или новому (UUIDv7 с сайта: в первых 48 битах — время начала, мс)."""
    for s in st:
        if key in (s["id"], s["uuid"]):
            return s
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", key):
        t = datetime.fromtimestamp(int(key.replace("-", "")[:12], 16) / 1000, timezone.utc).replace(tzinfo=None)
        best = min(st, key=lambda s: abs((datetime.fromisoformat(s["start"]) - t).total_seconds()), default=None)
        if best and abs((datetime.fromisoformat(best["start"]) - t).total_seconds()) < 120:
            return best
    return None


def clips_since(slug, start):
    """Клипы от новых к старым, пока не дойдём до начала стрима (раньше его клипов быть не может)."""
    cursor, res = None, []
    while True:
        url = f"https://kick.com/api/v2/channels/{slug}/clips?sort=date&time=all"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        d = get(url)
        res += d.get("clips", [])
        cursor = d.get("nextCursor")
        print(f"\rлистаю клипы: {len(res)}", end="", file=sys.stderr, flush=True)
        if not cursor or not d.get("clips") or res[-1]["created_at"][:19].replace("T", " ") < start:
            print(file=sys.stderr)
            return res


def views(c):
    return c.get("views") or c.get("view_count") or 0


def span(c, start):
    """Отрезок клипа в секундах от начала стрима — по времени эфира из плейлиста."""
    try:
        t = fetch(c["clip_url"], accept="*/*")
    except OSError:
        return None
    pdt = re.findall(r"PROGRAM-DATE-TIME:(\S+)", t)
    durs = re.findall(r"EXTINF:([\d.]+)", t)
    if not pdt:
        return None
    a = datetime.fromisoformat(pdt[0].replace("Z", "")[:26])
    b = datetime.fromisoformat(pdt[-1].replace("Z", "")[:26]) + timedelta(seconds=float(durs[-1]))
    return (a - start).total_seconds(), (b - start).total_seconds()


def collect(slug, s, fresh):
    cache = CACHE / f"{slug}_{s['id']}.json"
    if cache.exists() and not fresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    cl = [c for c in clips_since(slug, s["start"]) if c["livestream_id"] == s["id"]]
    start = datetime.fromisoformat(s["start"].replace(" ", "T"))
    print(f"читаю время эфира у {len(cl)} клипов…", file=sys.stderr)
    with ThreadPoolExecutor(16) as ex:
        spans = list(ex.map(lambda c: span(c, start), cl))
    res = [{"id": c["id"], "title": c["title"], "views": views(c), "s": sp[0], "e": sp[1]}
           for c, sp in zip(cl, spans) if sp]
    CACHE.mkdir(exist_ok=True)
    cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def overlap(a, b):
    common = min(a["e"], b["e"]) - max(a["s"], b["s"])
    return max(0, common) / max(1, min(a["e"] - a["s"], b["e"] - b["s"]))


def moments(cl):
    # лидеры — сначала короткие клипы с большими просмотрами: длинный лидер собрал бы соседние панчи
    groups = []
    for c in sorted(cl, key=lambda c: (c["e"] - c["s"] > SHORT_MAX, -c["views"])):
        for g in groups:
            if overlap(g[0], c) >= OVERLAP:
                g.append(c)
                break
        else:
            groups.append([c])
    res = []
    for g in groups:
        lo, hi = int(min(c["s"] for c in g)), int(max(c["e"] for c in g))
        cov = [sum(c["s"] <= t < c["e"] for c in g) for t in range(lo, hi)]
        m = max(cov)
        p = lo + next(i for i, v in enumerate(cov) if v >= PUNCH * m) + SHIFT
        has = [c for c in g if c["s"] <= p < c["e"]] or g
        shorts = [c for c in has if c["e"] - c["s"] <= SHORT_MAX] or has
        quick = [c for c in shorts if LEAD[0] <= p - c["s"] <= LEAD[1]]
        short = (max(quick, key=lambda c: c["views"]) if quick
                 else min(shorts, key=lambda c: (abs(p - c["s"] - 3), -c["views"])))
        long_ = max(has, key=lambda c: min(p - c["s"], c["e"] - c["s"] - (p - c["s"])))
        res.append({"punch": p, "n": len(g), "short": short, "long": long_})
    return res


def hms(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def ms(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("stream", nargs="?")
    ap.add_argument("--channel", default="kalatay3")
    ap.add_argument("--time", action="store_true")
    ap.add_argument("--min-clips", type=int, default=2)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()

    st = streams(a.channel)
    if not a.stream:
        print(f"Стримы {a.channel} (время UTC):")
        for i, s in enumerate(st, 1):
            print(f"{i:>3}. {s['start'][:16]}  {s['hours']:>4} ч  {s['title']}")
        return

    q = a.stream
    if "kick.com/" in q:  # ссылка: kick.com/<канал>/videos/<uuid> или kick.com/<канал>
        parts = q.split("kick.com/")[1].split("?")[0].strip("/").split("/")
        if parts[0] != a.channel:
            a.channel, st = parts[0], streams(parts[0])
        q = parts[2] if len(parts) > 2 and parts[1] == "videos" else "1"
    if q.isdigit() and int(q) <= len(st):
        s = st[int(q) - 1]
    else:
        s = find_stream(st, q) or next((s for s in st if s["start"].startswith(q)), None)
        if not s:
            sys.exit("Стрим не найден. Запусти без аргументов — будет список.")

    cl = collect(a.channel, s, a.fresh)
    link = lambda c: f"https://kick.com/{a.channel}/clips/{c['id']}"
    print(f"Стрим {s['start'][:16]} UTC — {s['title']}")
    if a.raw:
        print(f"Клипов: {len(cl)}\n")
        for c in sorted(cl, key=lambda c: -c["views"]):
            print(f"{c['views']:>6} просм.  {int(c['e'] - c['s']):>3} с  {hms(c['s'])}  {link(c)}")
        return

    ms_ = [m for m in moments(cl) if m["n"] >= a.min_clips]
    ms_.sort(key=(lambda m: m["punch"]) if a.time else (lambda m: -m["n"]))
    print(f"Клипов: {len(cl)} → моментов (от {a.min_clips} клипов): {len(ms_)}\n")
    for i, m in enumerate(ms_, 1):
        sh, lg, p = m["short"], m["long"], m["punch"]
        print(f"{i}. панч {hms(p)}  — заклипали {m['n']} раз")
        print(f"   короткий: {int(sh['e'] - sh['s'])} с, {sh['views']} просм., панч на {ms(p - sh['s'])}  {link(sh)}")
        if lg["id"] != sh["id"]:
            print(f"   длинный:  {int(lg['e'] - lg['s'])} с, панч на {ms(p - lg['s'])}  {link(lg)}")
        print()


if __name__ == "__main__":
    main()
