"""Клипы канала Twitch по стримам — склеенные в моменты (панчи). Как kick_clips.py, только Twitch.

  python tools/twitch_clips.py <канал>                          — стримы канала (номер, дата, длина, название)
  python tools/twitch_clips.py https://www.twitch.tv/videos/<id>  — моменты стрима по ссылке на запись
  python tools/twitch_clips.py https://www.twitch.tv/<канал>      — то же, последний стрим
  python tools/twitch_clips.py <канал> 2                        — по номеру из списка (1 = последний)
  --time, --min-clips N, --raw, --fresh — как в kick_clips.py

Откуда время (17.10.2026):
  - у клипа Twitch прямо есть videoOffsetSeconds — секунда начала в записи стрима; плейлисты не нужны;
  - API — открытый GraphQL сайта (gql.twitch.tv, публичный Client-ID, как у yt-dlp), без ключей;
  - вторая страница любого списка там требует integrity-токен, поэтому листать нельзя: время стрима режется
    на окна (startAt/endAt по времени создания клипа), окно со 100 клипами — пополам, пока не влезет в одну;
  - клипы делают и из записи после стрима — поэтому окна идут до 7 дней после конца стрима.
Склейка в моменты, «короткий» и «длинный» — те же правила, что в kick_clips.py (moments оттуда).

Канал без записей (у anarabdullaev они выключены): у клипа нет записи и videoOffsetSeconds, но есть номер эфира
(broadcast). Тогда стримы — эфиры за 14 дней по клипам, время клипа — когда его создали (конец клипа примерно
там же), отсчёт — от первого клипа эфира. Момент = клипы, созданные с промежутком до 45 с; точной секунды панча
нет — «короткий» = самый просматриваемый клип до 60 с, «длинный» = самый длинный.
Сбор стрима — секунды; результат сохраняется в tools/kick_cache/twitch_*.json.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # kick_clips рядом (и при python -I)
from kick_clips import SHORT_MAX, hms, moments, ms  # noqa: E402

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный id самого сайта twitch.tv
CACHE = Path(__file__).parent / "kick_cache"
AFTER = timedelta(days=7)     # клипы из записи, сделанные после стрима
MIN_WINDOW = timedelta(seconds=20)
NO_VOD_DAYS = 14              # канал без записей: эфиры за столько дней
GAP = 45                      # без записи: клипы, созданные с промежутком до GAP с, — один момент

CLIPS_Q = """query($l:String!,$a:Time,$b:Time){ user(login:$l){ clips(first:100,
  criteria:{startAt:$a, endAt:$b, period:ALL_TIME}){ edges{ node{ slug title viewCount durationSeconds
  videoOffsetSeconds createdAt video{ id } broadcast{ id } } } pageInfo{ hasNextPage } } } }"""


def gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL, data=body, headers={"Client-ID": CLIENT_ID, "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            break
        except (TimeoutError, OSError):
            if attempt == 4:
                raise
            time.sleep(2)
    if d.get("errors"):
        raise OSError(f"Twitch: {d['errors'][0].get('message')}")
    return d["data"]


def stream_of(v, login):
    return {
        "id": v["id"],
        "login": login,
        "title": (v.get("title") or "").strip(),
        "start": v["createdAt"][:19].replace("T", " "),  # UTC, как у kick_clips
        "hours": round((v.get("lengthSeconds") or 0) / 3600, 1),
        "length": v.get("lengthSeconds") or 0,
    }


def streams(login):
    """Записи стримов; у канала без записей — эфиры по клипам (broadcasts)."""
    d = gql("""query($l:String!){ user(login:$l){ videos(first:100, type:ARCHIVE){ edges{ node{
        id title createdAt lengthSeconds } } } } }""", {"l": login})
    if not d["user"]:
        raise OSError(f"нет канала {login}")
    return [stream_of(e["node"], login) for e in d["user"]["videos"]["edges"]] or broadcasts(login)


def video(vid):
    """Запись по id → стрим (с каналом) или None."""
    d = gql("query($id:ID!){ video(id:$id){ id title createdAt lengthSeconds owner{ login } } }", {"id": vid})
    v = d["video"]
    return stream_of(v, v["owner"]["login"]) if v and v.get("owner") else None


def window(login, a, b):
    """Клипы канала, созданные в [a, b): одна страница; полная — делим окно пополам."""
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in range(3):  # Twitch иногда отвечает «service timeout» — повтор, потом окно помельче
        try:
            cl = gql(CLIPS_Q, {"l": login, "a": iso(a), "b": iso(b)})["user"]["clips"]
            break
        except OSError as e:
            if "timeout" not in str(e) and "server error" not in str(e):
                raise
            if attempt == 2:
                if b - a <= MIN_WINDOW:
                    raise
                m = a + (b - a) / 2
                return window(login, a, m) + window(login, m, b)
            time.sleep(2 + attempt * 3)
    if cl["pageInfo"]["hasNextPage"] and b - a > MIN_WINDOW:
        m = a + (b - a) / 2
        return window(login, a, m) + window(login, m, b)
    return [e["node"] for e in cl["edges"]]


def clips_between(login, a, b, step):
    wins = []
    while a < b:
        wins.append((a, min(a + step, b)))
        a += step
    with ThreadPoolExecutor(4) as ex:
        parts = list(ex.map(lambda w: window(login, *w), wins))
    seen = {}
    for c in (c for p in parts for c in p):
        seen.setdefault(c["slug"], c)
    return list(seen.values())


def created(c):
    return datetime.fromisoformat(c["createdAt"][:19])


def broadcasts(login):
    """Канал без записей: эфиры за NO_VOD_DAYS дней по клипам, новые первыми. Клипы — сразу в стриме."""
    b = datetime.now(timezone.utc)
    by = defaultdict(list)
    for c in clips_between(login, b - timedelta(days=NO_VOD_DAYS), b, timedelta(hours=6)):
        if (c.get("broadcast") or {}).get("id"):
            by[c["broadcast"]["id"]].append(c)
    last = (gql("query($l:String!){ user(login:$l){ lastBroadcast{ id title } } }", {"l": login})["user"]
            or {}).get("lastBroadcast") or {}
    res = []
    for bid, cs in by.items():
        t0 = min(map(created, cs))
        cl = [{"id": c["slug"], "title": c["title"], "views": c["viewCount"],
               "e": (created(c) - t0).total_seconds()} for c in cs]
        for c, src in zip(cl, cs):
            c["s"] = c["e"] - src["durationSeconds"]
        length = max(c["e"] for c in cl)
        res.append({"id": f"b{bid}", "login": login, "approx": True, "clips": cl,
                    "title": (last.get("title") or "").strip() if last.get("id") == bid else "эфир без записи",
                    "start": t0.strftime("%Y-%m-%d %H:%M:%S"), "hours": round(length / 3600, 1), "length": length})
    return sorted(res, key=lambda s: s["start"], reverse=True)


def collect(s, fresh):
    if s.get("approx"):
        return s["clips"]  # уже собраны вместе со списком эфиров
    cache = CACHE / f"twitch_{s['login']}_{s['id']}.json"
    if cache.exists() and not fresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    a = datetime.fromisoformat(s["start"]).replace(tzinfo=timezone.utc)
    b = min(a + timedelta(seconds=s["length"]) + AFTER, datetime.now(timezone.utc))
    res = [{"id": c["slug"], "title": c["title"], "views": c["viewCount"],
            "s": c["videoOffsetSeconds"], "e": c["videoOffsetSeconds"] + c["durationSeconds"]}
           for c in clips_between(s["login"], a, b, timedelta(hours=1))
           if (c.get("video") or {}).get("id") == s["id"] and c.get("videoOffsetSeconds") is not None]
    CACHE.mkdir(exist_ok=True)
    cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def moments_of(s, cl):
    """Моменты стрима: с записью — правила kick_clips; без записи — кучки по времени создания клипа.
    У моментов без записи "approx": True, "punch" — когда создан первый клип (от первого клипа эфира)."""
    if not s.get("approx"):
        return moments(cl)
    groups = []
    for c in sorted(cl, key=lambda c: c["e"]):
        if groups and c["e"] - groups[-1][-1]["e"] <= GAP:
            groups[-1].append(c)
        else:
            groups.append([c])
    res = []
    for g in groups:
        shorts = [c for c in g if c["e"] - c["s"] <= SHORT_MAX] or g
        res.append({"punch": g[0]["e"], "n": len(g), "approx": True,
                    "short": max(shorts, key=lambda c: c["views"]),
                    "long": max(g, key=lambda c: (c["e"] - c["s"], c["views"]))})
    return res


def parse_link(q):
    """twitch.tv/videos/<id> → (None, id); twitch.tv/<канал> → (канал, None); иначе None."""
    m = re.search(r"twitch\.tv/videos/(\d+)", q)
    if m:
        return None, m[1]
    m = re.search(r"(?:^|//|www\.|m\.)twitch\.tv/([A-Za-z0-9_]{2,25})/?(?:[?#]|$)", q)
    if m and m[1].lower() not in ("videos", "directory", "settings", "downloads", "search"):
        return m[1].lower(), None
    return None


def find(q, num=None):
    """Ссылка или канал (+ номер записи) → стрим."""
    k = parse_link(q) if "twitch.tv" in q else (q.lower(), None)
    if not k:
        return None
    login, vid = k
    if vid:
        return video(vid)
    st = streams(login)
    i = (num or 1) - 1
    return st[i] if 0 <= i < len(st) else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("num", nargs="?", type=int)
    ap.add_argument("--time", action="store_true")
    ap.add_argument("--min-clips", type=int, default=2)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--list", action="store_true", help="только список стримов канала")
    a = ap.parse_args()

    if a.list or ("twitch.tv" not in a.stream and a.num is None):
        login = parse_link(a.stream)[0] if "twitch.tv" in a.stream else a.stream.lower()
        st = streams(login)
        print(f"Стримы {login} (время UTC){' — записей нет, эфиры по клипам' if st and st[0].get('approx') else ''}:")
        for i, s in enumerate(st, 1):
            print(f"{i:>3}. {s['start'][:16]}  {s['hours']:>4} ч  {s['title']}")
        return

    s = find(a.stream, a.num)
    if not s:
        sys.exit("Запись не найдена (записи на Twitch живут 7–60 дней). Запусти с именем канала — будет список.")
    cl = collect(s, a.fresh)
    link = lambda c: f"https://clips.twitch.tv/{c['id']}"
    print(f"Стрим {s['start'][:16]} UTC — {s['title']}")
    if s.get("approx"):
        print("Записи нет: время — от первого клипа эфира, по времени создания клипов (±1 мин)")
    if a.raw:
        print(f"Клипов: {len(cl)}\n")
        for c in sorted(cl, key=lambda c: -c["views"]):
            print(f"{c['views']:>6} просм.  {int(c['e'] - c['s']):>3} с  {hms(c['s'])}  {link(c)}")
        return

    ms_ = [m for m in moments_of(s, cl) if m["n"] >= a.min_clips]
    ms_.sort(key=(lambda m: m["punch"]) if a.time else (lambda m: -m["n"]))
    print(f"Клипов: {len(cl)} → моментов (от {a.min_clips} клипов): {len(ms_)}\n")
    for i, m in enumerate(ms_, 1):
        sh, lg, p = m["short"], m["long"], m["punch"]
        if m.get("approx"):
            print(f"{i}. ~{hms(p)}  — заклипали {m['n']} раз")
            print(f"   короткий: {int(sh['e'] - sh['s'])} с, {sh['views']} просм.  {link(sh)}")
            if lg["id"] != sh["id"]:
                print(f"   длинный:  {int(lg['e'] - lg['s'])} с  {link(lg)}")
        else:
            print(f"{i}. панч {hms(p)}  — заклипали {m['n']} раз")
            print(f"   короткий: {int(sh['e'] - sh['s'])} с, {sh['views']} просм., панч на {ms(p - sh['s'])}  {link(sh)}")
            if lg["id"] != sh["id"]:
                print(f"   длинный:  {int(lg['e'] - lg['s'])} с, панч на {ms(p - lg['s'])}  {link(lg)}")
        print()


if __name__ == "__main__":
    main()
