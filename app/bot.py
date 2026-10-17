"""Бот-качалка для группы в Telegram (@downloader_youu_bot). План и решения — notes/plans.md.

В группу кидают ссылку (Kick, TikTok, YouTube, Twitch, Instagram без входа…) → бот скачивает
видео, если надо — режет, и отвечает видео на это сообщение. Claude не участвует, токены не тратит.

  python tools/shared_bot/bot.py           # запуск (так же запускает Планировщик)
  python tools/shared_bot/bot.py --setup   # найти id группы, куда добавлен бот
  tools/shared_bot/stop.bat                # выключить: бот ставит в закреп «🔴 Выключен» и выходит
  /stop в группе (только от владельца)     # то же самое
  /log в группе (только от владельца)      # лог за сутки файлом — переслать Диме, если сломалось

Форматы — на одной строке со ссылкой:
  ссылка                 целиком
  ссылка -14             убрать первые 14 с
  ссылка -5 -3           убрать 5 с в начале и 3 с в конце
  ссылка до 58           оставить первые 58 с
  ссылка 1:20-1:45       отрезок (м:сс); у длинных — 1:20:00-1:20:10 (ч:мм:сс)

Темы группы (бот создаёт сам, 17.10.2026):
  «Выбор клипов»  ссылка на запись стрима Kick (или на канал = последний стрим) → 10 моментов: превью
                  главного клипа в 360p + ссылка на длинный клип с панчем посередине; «ещё» — следующие 10.
                  Ответ на превью: «ок» / «+» — целиком, или обрезка как выше (-1 -5, до 20…) →
                  клип в полном качестве уходит в «Отобранные». Моменты — tools/kick_clips.py.
  остальные       обычная качалка, ответ в той же теме.

Секреты (не пересылать, в bgswap-kit не класть): tools/shared-bot-token.txt,
tools/shared_bot/bot.session, api_id/api_hash из tools/tg-config.json.
"""
import asyncio
import html
import json
import logging
import msvcrt
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
# Набор для других (share/downloader-bot-kit): рядом лежит kit.json — свои настройки в <набор>/data,
# программы в <набор>/bin, обновления сами с Диска Димы. Без kit.json — бот Димы в tools/shared_bot.
KIT_FILE = HERE / "kit.json"
KIT = json.loads(KIT_FILE.read_text(encoding="utf-8")) if KIT_FILE.exists() else None
if KIT:
    ROOT = HERE.parent
    DATA = ROOT / "data"
    TOOLS = None
    TOKEN_FILE = DATA / "token.txt"
    TG_CONFIG = DATA / "api.json"
    BIN = ROOT / "bin"
    FFMPEG = BIN / "ffmpeg.exe"
    KICK_CACHE = DATA / "kick_cache"
else:
    ROOT = DATA = HERE
    TOOLS = HERE.parent
    TOKEN_FILE = TOOLS / "shared-bot-token.txt"
    TG_CONFIG = TOOLS / "tg-config.json"
    BIN = HERE / "bin"
    FFMPEG = TOOLS / "fw" / "Faster-Whisper-XXL" / "ffmpeg.exe"
    KICK_CACHE = None  # как в kick_clips.py: tools/kick_cache
CONFIG = DATA / "config.json"
STATE = DATA / "state.json"
STOP_FLAG = DATA / "stop.flag"
LOCK = DATA / "bot.lock"
LOG = DATA / "bot.log"
SESSION = DATA / "bot"
YTDLP = BIN / "yt-dlp.exe"
YTDLP_PREV = BIN / "yt-dlp.prev.exe"
OWNER = "Дима"  # имя владельца в ошибках «… посмотрит»; в наборе — из config.json

DEFAULTS = {
    "group_id": None,           # id группы (--setup / --wizard)
    "owner_id": None,           # кто может /stop (по умолчанию — chat_id Димы из tg-config.json)
    "owner_name": "Дима",
    "out_dir": str(ROOT / "clips") if KIT else r"D:\kalatay-tt-insta",  # копия каждого клипа
    "tmp_dir": str(ROOT / "tmp") if KIT else r"D:\shared-bot-tmp",      # временные, чистятся после отправки
    "status_every": 60,         # как часто обновлять закреп, с
    "stale_after_min": 2,       # «если время старше N мин — бот выключен»
    "max_full_min": 15,         # длиннее — только с отрезком (записи стримов)
    "max_piece_min": 15,        # самый длинный отрезок
}

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: ffmpeg/yt-dlp без чёрных окон

PICK_TOPIC = "Выбор клипов"
DONE_TOPIC = "Отобранные"
PICK_BATCH = 10           # моментов за раз
PICK_MIN_CLIPS = 2        # момент, который заклипали один раз, — не показываем
KICK_CACHE_MAX_H = 1      # сохранённые клипы стрима старше часа — собрать заново (клипы ещё добавляются)
OK_WORDS = {"ок", "ok", "+", "да", "беру", "👍"}

HINT = ("<pre>kick.com/…/clip_AAA -14\n"
        "kick.com/…/videos/BBB 1:20:00-1:20:10\n"
        "kick.com/…/clip_CCC</pre>\n"
        "ещё: <code>до 58</code> · <code>-5 -3</code> · <code>1:20-1:45</code>")

URL_RE = re.compile(
    r"(https?://\S+|(?:www\.)?(?:kick\.com|(?:vm\.|vt\.)?tiktok\.com|instagram\.com|youtube\.com|"
    r"youtu\.be|(?:clips\.)?twitch\.tv)/\S+)", re.I)
TIME = r"\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?"

log = logging.getLogger("bot")


# ---------- настройки и состояние ----------

def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_config():
    c = dict(DEFAULTS)
    c.update(load_json(CONFIG, {}))
    if not c["owner_id"]:
        c["owner_id"] = load_json(TG_CONFIG, {}).get("chat_id")
    if not CONFIG.exists():
        save_json(CONFIG, c)
    return c


# ---------- разбор сообщения ----------

class CutError(Exception):
    pass


def secs(t):
    """«1:20:00», «1:20», «80», «12,5» → секунды. Лишние нули прощаем, 1:75 — нет."""
    parts = t.replace(",", ".").split(":")
    for p in parts[1:]:
        if float(p) >= 60:
            raise CutError(f"непонятный таймкод «{t}»")
    v = 0.0
    for p in parts:
        v = v * 60 + float(p)
    return v


def parse_cut(rest):
    """Текст после ссылки → {"start", "end", "tail"} в секундах (чего нет — того нет)."""
    s = rest.strip().lower().replace("—", "-").replace("–", "-")
    if not re.search(r"\d", s):  # пусто или просто комментарий («смешной момент») — целиком
        return {}
    m = re.fullmatch(rf"({TIME})\s*-\s*({TIME})", s)
    if m:
        a, b = secs(m[1]), secs(m[2])
        if b <= a:
            raise CutError("конец отрезка раньше начала")
        return {"start": a, "end": b}
    m = re.fullmatch(rf"до\s*({TIME})", s)
    if m:
        return {"end": secs(m[1])}
    m = re.fullmatch(rf"-\s*({TIME})(?:\s+-\s*({TIME}))?", s)
    if m:
        return {"start": secs(m[1]), "tail": secs(m[2]) if m[2] else 0.0}
    raise CutError(f"не понял «{rest.strip()}»")


def norm_url(u):
    u = u.rstrip(".,;:!?)»\"'")
    if not u.lower().startswith("http"):
        u = "https://" + u
    p = urlsplit(u)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = p.path.rstrip("/")
    if host in ("youtube.com", "music.youtube.com") and path == "/watch":
        v = parse_qs(p.query).get("v", [""])[0]
        return f"https://youtube.com/watch?v={v}"
    return f"https://{host}{path}"


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def parse_message(text):
    """→ список (url, cut | CutError, quote, quote_offset). Отрезок пишется на той же строке после
    своей ссылки; quote — этот кусок сообщения как есть, бот цитирует его в ответе."""
    jobs = []
    pos = 0
    for line in text.splitlines(keepends=True):
        found = list(URL_RE.finditer(line))
        for i, m in enumerate(found):
            end = found[i + 1].start() if i + 1 < len(found) else len(line)
            try:
                cut = parse_cut(line[m.end():end])
            except CutError as e:
                cut = e
            quote = line[m.start():end].rstrip()
            jobs.append((norm_url(m[1]), cut, quote, utf16_len(text[:pos + m.start()])))
        pos += len(line)
    return jobs


def cut_key(url, cut):
    r = lambda x: None if x is None else round(x, 2)
    return f"{url}|{r(cut.get('start'))}|{r(cut.get('end'))}|{r(cut.get('tail'))}"


def tc(x):
    x = int(round(x))
    h, m, s = x // 3600, x // 60 % 60, x % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def cut_label(cut):
    """Для подписи и имени файла."""
    if "end" in cut and "start" in cut:
        return f"{tc(cut['start'])}–{tc(cut['end'])}"
    if "end" in cut:
        return f"до {tc(cut['end'])}"
    if cut.get("start") or cut.get("tail"):
        parts = []
        if cut.get("start"):
            parts.append(f"−{cut['start']:g} с в начале")
        if cut.get("tail"):
            parts.append(f"−{cut['tail']:g} с в конце")
        return ", ".join(parts)
    return "целиком"


# ---------- внешние программы ----------

def run(cmd, timeout):
    log.info("RUN %s", " ".join(str(c) for c in cmd)[:400])
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


class JobError(Exception):
    pass


def explain(url, err):
    """Ошибка yt-dlp/ffmpeg → причина простыми словами (None — непонятная)."""
    e = err.lower()
    if "instagram" in url and any(w in e for w in ("login", "rate-limit", "cookies", "not available")):
        return "Instagram не пустил без входа"
    if "unsupported url" in e:
        return "этот сайт бот не знает"
    if "is live" in e or "live event" in e:
        return "это прямой эфир — дождись записи"
    if "private" in e:
        return "видео приватное"
    if "404" in e or "not found" in e or "does not exist" in e or "unavailable" in e:
        return "видео не найдено — удалено или ссылка не та"
    if "login" in e or "sign in" in e or "cookies" in e:
        return "сайт требует вход"
    if "403" in e or "forbidden" in e or "cloudflare" in e:
        return "сайт не пустил (защита) — попробуй позже"
    if "timed out" in e or "timeout" in e:
        return "сайт не ответил вовремя — кинь ещё раз"
    return None


def ytdlp(args, url, timeout):
    """yt-dlp; при непонятной ошибке — повтор на прошлой версии (если обновление что-то сломало)."""
    r = run([YTDLP, *args, url], timeout)
    if r.returncode == 0:
        return r
    if explain(url, r.stderr) is None and YTDLP_PREV.exists():
        log.warning("yt-dlp упал непонятно, пробую прошлую версию")
        r2 = run([YTDLP_PREV, *args, url], timeout)
        if r2.returncode == 0:
            log.warning("прошлая версия yt-dlp сработала")
            return r2
    log.error("yt-dlp: %s", r.stderr[-2000:])
    raise JobError(explain(url, r.stderr) or f"не скачал, {OWNER} посмотрит")


def update_ytdlp():
    BIN.mkdir(exist_ok=True)
    if not YTDLP.exists() and TOOLS:
        shutil.copy2(TOOLS / "yt-dlp.exe", YTDLP)
    shutil.copy2(YTDLP, YTDLP_PREV)
    try:
        r = run([YTDLP, "-U"], 180)
        log.info("yt-dlp -U: %s", (r.stdout + r.stderr).strip().splitlines()[-1:])
    except Exception as e:  # нет интернета и т.п. — работаем на старой
        log.warning("yt-dlp -U не вышло: %s", e)


def probe(path):
    out = run([FFMPEG, "-hide_banner", "-i", path], 60).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    v = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", out)
    fps = re.search(r"([\d.]+) fps", out)
    if not (d and v):
        raise JobError("скачался битый файл — кинь ещё раз")
    return {"duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]), "codec": v[1],
            "w": int(v[2]), "h": int(v[3]), "fps": float(fps[1]) if fps else 0}


ENC = ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]


def streamer_of(url, info):
    p = urlsplit(url)
    seg = [s for s in p.path.split("/") if s]
    if "kick.com" in p.netloc and seg:
        name = seg[0]
    elif "tiktok.com" in p.netloc and seg and seg[0].startswith("@"):
        name = seg[0][1:]
    else:
        name = info.get("channel") or info.get("uploader_id") or info.get("uploader") or "video"
    return re.sub(r"[^\w.-]", "_", str(name))[:40]


def process(url, cut, cfg, work):
    """Скачать и порезать. → (путь к mp4, сведения для подписи). Блокирующая — гоняется в потоке."""
    work.mkdir(parents=True, exist_ok=True)
    info = json.loads(ytdlp(["-J", "--no-playlist", "--no-warnings"], url, 120).stdout)
    if info.get("is_live"):
        raise JobError("это прямой эфир — дождись записи")
    dur = info.get("duration")
    start, end, tail = cut.get("start", 0.0), cut.get("end"), cut.get("tail", 0.0)
    if dur:
        if end is not None and end > dur + 0.5:
            raise JobError(f"в видео всего {tc(dur)}")
        if start + tail >= dur:
            raise JobError(f"после обрезки ничего не остаётся — в видео всего {tc(dur)}")
    out = work / "out.mp4"

    if dur and dur > cfg["max_full_min"] * 60:
        # Длинное (запись стрима): тянем из HLS только нужный кусок.
        if end is None:
            raise JobError(f"это длинное видео ({tc(dur)}) — укажи отрезок, например 1:20:00-1:20:10")
        if end - start > cfg["max_piece_min"] * 60:
            raise JobError(f"отрезок длиннее {cfg['max_piece_min']} мин — возьми кусок поменьше")
        fmts = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none") and f.get("url")]
        if not fmts:
            raise JobError("с этого сайта кусок длинного видео не вырезать")
        f = max(fmts, key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0), (f.get("tbr") or 0)))
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        if hdr:
            cmd += ["-headers", hdr]
        r = run(cmd + ["-ss", f"{start:.3f}", "-i", f["url"], "-t", f"{end - start:.3f}", *ENC, out], 1800)
        if r.returncode != 0 or not out.exists():
            log.error("ffmpeg кусок: %s", r.stderr[-2000:])
            raise JobError(explain(url, r.stderr) or f"не вырезал кусок, {OWNER} посмотрит")
    else:
        ytdlp(["--no-playlist", "--no-warnings", "--no-progress", "-S", "res,fps,vcodec:h264,acodec:aac",
               "--merge-output-format", "mp4", "--remux-video", "mp4",
               "--ffmpeg-location", FFMPEG.parent, "-o", str(work / "src.%(ext)s")], url, 1800)
        src = next(iter(sorted(work.glob("src.*"))), None)
        if not src:
            raise JobError(f"не скачал, {OWNER} посмотрит")
        p = probe(src)
        d = p["duration"]
        e = (min(end, d) if end is not None else d) - tail
        if e - start <= 0.2:
            raise JobError(f"после обрезки ничего не остаётся — в видео всего {tc(d)}")
        if start < 0.05 and e >= d - 0.05 and p["codec"] == "h264":
            shutil.move(src, out)                       # целиком, как скачано
        elif start < 0.05 and p["codec"] == "h264":    # только хвост — без пережатия
            r = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", f"{e:.3f}",
                     "-c", "copy", "-movflags", "+faststart", out], 600)
        else:                                          # начало — точно, с пережатием CRF 18
            r = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", src,
                     "-t", f"{e - start:.3f}", *ENC, out], 1800)
        if not out.exists():
            log.error("ffmpeg рез: %s", r.stderr[-2000:])
            raise JobError(f"не порезал, {OWNER} посмотрит")

    p = probe(out)
    if out.stat().st_size > 1950 * 2**20:
        raise JobError("получилось больше 2 ГБ — возьми кусок поменьше")
    thumb = work / "thumb.jpg"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1, p['duration'] / 2):.2f}",
         "-i", out, "-frames:v", "1", "-vf", "scale=320:-2", thumb], 60)
    p["thumb"] = thumb if thumb.exists() else None
    p["streamer"] = streamer_of(url, info)
    return out, p


# ---------- моменты стрима Kick (тема «Выбор клипов») ----------

def kick():
    """kick_clips.py: в наборе — рядом с ботом, у Димы — в tools/."""
    sys.path.insert(0, str(HERE if KIT else TOOLS))
    import kick_clips as kc
    if KICK_CACHE:
        kc.CACHE = KICK_CACHE
    return kc


def kick_stream_link(url):
    """kick.com/<канал>/videos/<uuid> → (канал, uuid); kick.com/<канал> → (канал, None); иначе None."""
    p = urlsplit(url)
    seg = [s for s in p.path.split("/") if s]
    if "kick.com" not in p.netloc or not seg:
        return None
    if len(seg) == 1:
        return seg[0], None
    if len(seg) == 3 and seg[1] == "videos":
        return seg[0], seg[2]
    return None


def stream_moments(channel, uuid):
    """→ (стрим, число клипов, моменты от самых заклипанных). Блокирующая — гоняется в потоке."""
    kc = kick()
    st = kc.streams(channel)
    s = kc.find_stream(st, uuid) if uuid else (st[0] if st else None)
    if not s:
        raise JobError("стрим не найден — нужна ссылка на запись: kick.com/<канал>/videos/…")
    cache = kc.CACHE / f"{channel}_{s['id']}.json"
    fresh = not cache.exists() or time.time() - cache.stat().st_mtime > KICK_CACHE_MAX_H * 3600
    cl = kc.collect(channel, s, fresh)
    ms = sorted((m for m in kc.moments(cl) if m["n"] >= PICK_MIN_CLIPS), key=lambda m: -m["n"])
    return s, len(cl), ms


def make_preview(clip_id, work):
    """Главный клип целиком в 360p (~1–2 МБ) — посмотреть прямо в Telegram.
    Прямо из плейлиста клипа одним ffmpeg: ~10 с против ~40 через yt-dlp."""
    kc = kick()
    work.mkdir(parents=True, exist_ok=True)
    try:
        d = kc.get(f"https://kick.com/api/v2/clips/{clip_id}")
    except OSError:
        raise JobError("Kick не отдал клип")
    m3u8 = (d.get("clip") or d).get("clip_url")
    if not m3u8:
        raise JobError("клип удалён")
    prev = work / "preview.mp4"
    r = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", m3u8, "-vf", "scale=-2:360",
             "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", prev], 600)
    if r.returncode != 0 or not prev.exists():
        log.error("превью: %s", r.stderr[-1000:])
        raise JobError("не сделал превью")
    q = probe(prev)
    thumb = work / "thumb.jpg"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "3", "-i", prev, "-frames:v", "1", thumb], 60)
    q["thumb"] = thumb if thumb.exists() else None
    return prev, q


# ---------- Telegram ----------

def esc(s):
    return html.escape(s, quote=False)


def link(url, word="ссылка"):
    return f'<a href="{html.escape(url, quote=True)}">{word}</a>'


def hms(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def ms(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def video_media(client_file, thumb_file, p, name):
    from telethon.tl import types
    return types.InputMediaUploadedDocument(
        file=client_file, mime_type="video/mp4", thumb=thumb_file,
        attributes=[types.DocumentAttributeVideo(duration=round(p["duration"], 2), w=p["w"], h=p["h"],
                                                 supports_streaming=True),
                    types.DocumentAttributeFilename(f"{name}.mp4")])


class Bot:
    def __init__(self, cfg):
        from telethon import TelegramClient
        self.cfg = cfg
        tg = load_json(TG_CONFIG, {})
        self.client = TelegramClient(str(SESSION), int(tg["api_id"]), tg["api_hash"], catch_up=True)
        self.state = load_json(STATE, {"status_msg_id": None, "cache": {}})
        self.queue = asyncio.Queue()
        self.pick_queue = asyncio.Queue()  # сбор моментов стрима — отдельно, чтобы не держать качалку
        self.busy = 0  # 1, пока worker делает видео (для номера в очереди)
        self.since = datetime.now()
        self.group = None
        self.stopping = asyncio.Event()

    def save(self):
        save_json(STATE, self.state)

    def status_text(self, on):
        now = datetime.now()
        if on:
            return (f"🟢 На связи: {now:%d.%m %H:%M}\n"
                    f"если время старше {self.cfg['stale_after_min']} мин — бот выключен\n\n{HINT}")
        return f"🔴 Выключен в {now:%d.%m %H:%M}\n\n{HINT}"

    async def set_status(self, on):
        from telethon import errors
        mid = self.state.get("status_msg_id")
        text = self.status_text(on)
        if mid:
            try:
                await self.client.edit_message(self.group, mid, text, parse_mode="html", link_preview=False)
                return
            except errors.MessageNotModifiedError:
                return
            except errors.FloodWaitError as e:
                log.warning("FloodWait %s с", e.seconds)
                return
            except Exception as e:  # закреп удалили — создадим новый
                log.warning("закреп не правится (%s), создаю новый", e)
        msg = await self.client.send_message(self.group, text, parse_mode="html", link_preview=False,
                                             silent=True)
        self.state["status_msg_id"] = msg.id
        self.save()
        try:
            service = await self.client.pin_message(self.group, msg, notify=False)
            if service:  # служебное «бот закрепил…» — убрать, чтобы не мусорить
                await self.client.delete_messages(self.group, service)
        except Exception as e:
            log.warning("не закрепил: %s — нужен админ с правом закреплять", e)

    async def send(self, text, media=None, topic=None, reply_to=None, buttons=None):
        """Новое сообщение: text — HTML, ссылка в нём словом «ссылка». topic — id темы (None — общая),
        reply_to — ответ на сообщение внутри этой темы, buttons — кнопки под сообщением (Button.inline)."""
        from telethon.extensions import html as tg_html
        from telethon.tl import functions, types
        msg, ent = tg_html.parse(text)
        rt = None
        if reply_to or topic:
            rt = types.InputReplyToMessage(reply_to_msg_id=reply_to or topic,
                                           top_msg_id=topic if reply_to and topic else None)
        markup = self.client.build_reply_markup(buttons) if buttons else None
        # в своих темах бот пишет беззвучно (просьба Димы 17.10.2026): мьютить тему за людей Telegram не даёт
        silent = bool(topic) and topic in (self.pick_topic, self.done_topic)
        if media is None:
            req = functions.messages.SendMessageRequest(peer=self.peer, message=msg, entities=ent,
                                                        no_webpage=True, reply_to=rt, reply_markup=markup,
                                                        silent=silent)
        else:
            req = functions.messages.SendMediaRequest(peer=self.peer, media=media, message=msg, entities=ent,
                                                      reply_to=rt, reply_markup=markup, silent=silent)
        res = await self.client(req)
        return self.client._get_response_message(req, res, self.peer)

    async def edit(self, msg_id, text):
        from telethon.extensions import html as tg_html
        from telethon.tl import functions
        msg, ent = tg_html.parse(text)
        try:
            await self.client(functions.messages.EditMessageRequest(peer=self.peer, id=msg_id, message=msg,
                                                                    entities=ent, no_webpage=True))
        except Exception as e:
            log.warning("не отредактировал %s: %s", msg_id, e)

    async def tell_owner(self, text):
        """В личку владельцу, не чаще раза в 12 ч на одну тему (Дима должен один раз нажать Start у бота)."""
        last = self.state.setdefault("told", {}).get(text, 0)
        if time.time() - last < 12 * 3600:
            return
        try:
            await self.client.send_message(self.cfg["owner_id"], f"⚠️ {text}")
            self.state["told"][text] = time.time()
            self.save()
        except Exception as e:
            log.warning("не написал владельцу (нажал ли он Start у бота?): %s", e)

    async def delete(self, msg_id):
        try:
            await self.client.delete_messages(self.group, [msg_id])
        except Exception as e:
            log.warning("не удалил сообщение %s: %s", msg_id, e)

    async def ensure_topics(self):
        """Темы «Выбор клипов» и «Отобранные»: бот создаёт их сам один раз и запоминает id в state.json
        (список тем ботам Telegram не отдаёт). Удалили тему — стереть её из state["topics"] и перезапустить."""
        from telethon.tl import functions
        have = self.state.setdefault("topics", {})
        for title in (PICK_TOPIC, DONE_TOPIC):
            if title not in have:
                upd = await self.client(functions.messages.CreateForumTopicRequest(peer=self.peer, title=title))
                have[title] = next(u.id for u in upd.updates if hasattr(u, "id"))
                self.save()
                log.info("создал тему «%s» → %s", title, have[title])
        self.pick_topic, self.done_topic = have[PICK_TOPIC], have[DONE_TOPIC]

    @staticmethod
    def where(event):
        """→ (тема, на какое сообщение ответили). Тема None — общая."""
        r = event.message.reply_to
        if not r:
            return None, None
        if getattr(r, "forum_topic", False):
            if r.reply_to_top_id:
                return r.reply_to_top_id, r.reply_to_msg_id
            return r.reply_to_msg_id, None
        return None, r.reply_to_msg_id

    async def send_log(self, event):
        """/log от владельца: лог за последние сутки файлом — переслать автору бота, если что-то сломалось."""
        since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        keep, lines = False, []
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line):
                keep = line[:19] >= since
            if keep:  # строки без даты (ошибки) — к своей записи
                lines.append(line)
        ver = KIT["version"] if KIT else "бот Димы"
        out = Path(self.cfg["tmp_dir"]) / f"bot-log_{datetime.now():%Y-%m-%d_%H-%M}.txt"
        out.write_text(f"версия: {ver}\n\n" + "\n".join(lines[-20000:]), encoding="utf-8")
        try:
            await self.client.send_file(self.group, str(out), reply_to=event.id, force_document=True,
                                        caption=f"Лог за сутки · версия {ver}. Что-то сломалось — перешли этот файл "
                                                f"автору бота.")
        finally:
            out.unlink(missing_ok=True)

    async def on_message(self, event):
        """На каждую ссылку сразу «⏳ В очереди · ссылка», сообщение человека удаляется.
        Готово → видео, «⏳» удаляется (текст в видео Telegram превратить не даёт); ошибка → «⏳» → «❌».
        Тема «Выбор клипов» — своя логика (on_pick)."""
        if event.chat_id != self.cfg["group_id"]:
            return
        text = event.raw_text or ""
        cmd = text.strip().lower().split("@")[0]
        if cmd == "/stop":
            if event.sender_id == self.cfg["owner_id"]:
                self.stopping.set()
            return
        if cmd == "/log":
            if event.sender_id == self.cfg["owner_id"]:
                await self.send_log(event)
            return
        topic, replied = self.where(event)
        if topic and topic == self.pick_topic and await self.on_pick(event, text, replied):
            return
        jobs = parse_message(text)
        if not jobs:
            return  # обычная переписка — не трогаем
        for url, cut, _quote, _offset in jobs:
            if isinstance(cut, CutError):
                await self.send(f"❌ {esc(str(cut))} — пример: ссылка 1:20-1:45 · {link(url)}", topic=topic)
            else:
                wait = self.queue.qsize() + self.busy
                ph = await self.send(f"⏳ В очереди{f': {wait + 1}' if wait else ''} · {link(url)}", topic=topic)
                await self.queue.put({"ph": ph.id, "url": url, "cut": cut, "topic": topic})
        await self.delete(event.id)

    async def take(self, pick_msg, cut, sender_id, sender, reply_to):
        """Превью выбрано (ответом или кнопкой) → в очередь: полное качество в «Отобранные»."""
        pick = self.state["picks"][str(pick_msg)]
        who = self.cfg["owner_name"] if sender_id == self.cfg["owner_id"] else (
            getattr(sender, "first_name", None) or getattr(sender, "username", None) or "кто-то")
        wait = self.queue.qsize() + self.busy
        ph = await self.send(f"⏳ В очереди{f': {wait + 1}' if wait else ''} · {cut_label(cut)}",
                             topic=self.pick_topic, reply_to=reply_to)
        await self.queue.put({"ph": ph.id, "url": pick["url"], "cut": cut, "topic": self.done_topic,
                              "pick": pick, "pick_msg": pick_msg, "who": who})

    async def on_button(self, event):
        """Кнопка «✅ Беру целиком» под превью."""
        if event.chat_id != self.cfg["group_id"]:
            return
        if event.data == b"more":  # «Ещё 10» под итогом пачки — убрать кнопку и прислать следующие
            await event.answer("Шлю ещё ✅")
            msg = await event.get_message()
            await self.edit(event.message_id, esc(msg.message or "Ещё"))
            await self.pick_queue.put({"kind": "more", "reply_to": event.message_id})
            return
        if event.data != b"take":
            return
        mid = event.message_id
        if str(mid) not in self.state.get("picks", {}):
            await event.answer("Это превью бот уже не помнит — кинь стрим заново")
            return
        busy = self.state.setdefault("taking", [])
        if mid in busy:
            await event.answer("Уже в работе")
            return
        busy.append(mid)
        del busy[:-50]
        await event.answer("Беру ✅")
        await self.take(mid, {}, event.sender_id, await event.get_sender(), reply_to=mid)

    async def on_pick(self, event, text, replied):
        """Тема «Выбор клипов». → True, если сообщение разобрано здесь."""
        t = text.strip().lower()
        picks = self.state.setdefault("picks", {})
        if replied and str(replied) in picks:  # ответ на превью: «ок» или обрезка
            if t in OK_WORDS:
                cut = {}
            else:
                try:
                    cut = parse_cut(text)
                except CutError as e:
                    await self.send(f"❌ {esc(str(e))} — пример: «ок», «-1 -5», «до 20»",
                                    topic=self.pick_topic, reply_to=event.id)
                    return True
            await self.take(replied, cut, event.sender_id, await event.get_sender(), reply_to=event.id)
            return True
        if t in ("ещё", "еще", "/more", "more"):
            await self.pick_queue.put({"kind": "more", "reply_to": event.id})
            return True
        for url, cut, _q, _o in parse_message(text):
            k = kick_stream_link(url)
            if k and not cut:
                ph = await self.send(f"⏳ Собираю клипы стрима · {link(url)}\nпервый раз — до ~5 мин",
                                     topic=self.pick_topic, reply_to=event.id)
                await self.pick_queue.put({"kind": "stream", "channel": k[0], "uuid": k[1], "url": url,
                                           "ph": ph.id})
                return True
        return False  # прочие ссылки — обычной качалкой, ответ в этой же теме

    async def pick_worker(self):
        n = 0
        while True:
            job = await self.pick_queue.get()
            try:
                if job["kind"] == "stream":
                    s, total, moments = await asyncio.to_thread(stream_moments, job["channel"], job["uuid"])
                    self.state["pick"] = {"channel": job["channel"], "title": s["title"], "start": s["start"],
                                          "moments": moments, "next": 0}
                    self.save()
                    await self.edit(job["ph"], f"📺 <b>{esc(s['title'])}</b> · {s['start'][:10]} · "
                                               f"{link(job['url'])}\nклипов {total} → моментов {len(moments)}, "
                                               f"самые заклипанные первыми")
                elif not self.state.get("pick"):
                    await self.send("Сначала кинь ссылку на запись стрима", topic=self.pick_topic,
                                    reply_to=job["reply_to"])
                    continue
                n = await self.send_batch(n)
            except JobError as e:
                log.warning("моменты: %s · %s", e, job.get("url"))
                if job.get("ph"):
                    await self.edit(job["ph"], f"❌ {esc(str(e))} · {link(job['url'])}")
            except Exception:
                log.exception("сбор моментов")
                if job.get("ph"):
                    await self.edit(job["ph"], f"❌ Не собрал клипы, {OWNER} посмотрит · {link(job['url'])}")

    async def send_batch(self, n):
        """Следующие PICK_BATCH моментов: превью 360p + подпись."""
        from telethon import Button
        st = self.state["pick"]
        ch, moments = st["channel"], st["moments"]
        picks = self.state.setdefault("picks", {})
        i0 = st["next"]
        for i in range(i0, min(i0 + PICK_BATCH, len(moments))):
            m = moments[i]
            sh, lg, p = m["short"], m["long"], m["punch"]
            url = f"https://kick.com/{ch}/clips/{sh['id']}"
            long_url = f"https://kick.com/{ch}/clips/{lg['id']}"
            caption = (f"<b>{i + 1}/{len(moments)}</b> · панч {hms(p)} · заклипали {m['n']} раз\n"
                       f"клип {int(sh['e'] - sh['s'])} с, панч на {ms(p - sh['s'])} · {link(url)}\n"
                       f"длинный {int(lg['e'] - lg['s'])} с, панч на {ms(p - lg['s'])} · {link(long_url)}")
            n += 1
            work = Path(self.cfg["tmp_dir"]) / f"pick{n}"
            try:
                prev, q = await asyncio.to_thread(make_preview, sh["id"], work)
                media = video_media(await self.client.upload_file(str(prev)),
                                    await self.client.upload_file(str(q["thumb"])) if q["thumb"] else None,
                                    q, f"{ch}_{i + 1}")
                msg = await self.send(caption, media, topic=self.pick_topic,
                                      buttons=[Button.inline("✅ Беру целиком", b"take")])
            except JobError as e:
                msg = await self.send(f"{caption}\n❌ превью не вышло: {esc(str(e))}", topic=self.pick_topic)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            picks[str(msg.id)] = {"url": url, "long": long_url, "punch": p, "n": m["n"], "caption": caption,
                                  "title": st["title"]}
            st["next"] = i + 1
            self.save()
        if len(picks) > 500:  # старые превью забываем
            for k in sorted(picks, key=int)[:-500]:
                del picks[k]
            self.save()
        left = len(moments) - st["next"]
        await self.send(f"«✅ Беру целиком» под превью — клип уйдёт в «{DONE_TOPIC}»; "
                        f"обрезать — ответь на превью: «-1 -5», «до 20».\n"
                        + (f"Осталось моментов: {left}" if left else "Это все моменты стрима."),
                        topic=self.pick_topic,
                        buttons=[Button.inline(f"➡️ Ещё {min(PICK_BATCH, left)}", b"more")] if left else None)
        return n

    async def worker(self):
        from telethon import utils
        n = 0
        while True:
            job = await self.queue.get()
            ph, url, cut, topic = job["ph"], job["url"], job["cut"], job["topic"]
            pick = job.get("pick")  # отбор из «Выбора клипов»: видео — в «Отобранные», «⏳» — в «Выборе»
            self.busy = 1
            key = cut_key(url, cut)
            n += 1
            work = Path(self.cfg["tmp_dir"]) / f"job{n}"
            try:
                cached = None if pick else self.state["cache"].get(key)
                if cached:  # уже качали ровно это — переслать без закачки
                    old = await self.client.get_messages(self.group, ids=cached)
                    if old and old.media:
                        base = (old.message or "").removesuffix(" · ссылка")
                        await self.send(f"{esc(base)} · {link(url)}", utils.get_input_media(old.media),
                                        topic=topic)
                        await self.delete(ph)
                        log.info("повтор %s", key)
                        continue
                await self.edit(ph, f"⏳ В работе · {link(url)}")
                out, p = await asyncio.to_thread(process, url, cut, self.cfg, work)
                await self.edit(ph, f"⏳ Отправляю · {link(url)}")
                q = f"{p['h']}p{round(p['fps'])}" if p["fps"] >= 50 else f"{p['h']}p"
                caption = f"{esc(p['streamer'])} · {cut_label(cut)} · {q} · {link(url)}"
                if pick:
                    caption = (f"{esc(pick['title'])}\nпанч {hms(pick['punch'])} · заклипали {pick['n']} раз · "
                               f"выбрал {esc(job['who'])}\n{caption} · {link(pick['long'], 'длинный')}")
                media = video_media(await self.client.upload_file(str(out)),
                                    await self.client.upload_file(str(p["thumb"])) if p["thumb"] else None,
                                    p, p["streamer"])
                sent = await self.send(caption, media, topic=topic)
                if pick:
                    await self.edit(ph, f"✅ в «{DONE_TOPIC}» · {cut_label(cut)} · {esc(job['who'])}")
                    await self.edit(job["pick_msg"], f"✅ выбрал {esc(job['who'])}\n{pick['caption']}")
                else:
                    await self.delete(ph)
                    self.state["cache"][key] = sent.id
                    self.save()
                await asyncio.to_thread(self.copy_out, out, p["streamer"], cut)
                log.info("готово %s → %s", key, sent.id)
            except JobError as e:
                await self.edit(ph, f"❌ Не скачал: {esc(str(e))} · {link(url)}")
                if "Instagram" in str(e):
                    await self.tell_owner(
                        "Instagram перестал пускать без входа — ссылки с Instagram сейчас не скачиваются. "
                        "Обычно через несколько часов проходит само; остальные сайты работают." if KIT else
                        "Instagram перестал пускать без входа — пора добавить запасные "
                        "аккаунты (этап 2 в notes/plans.md, файл accounts/instagram-bot-pool.txt).")
            except Exception:
                log.exception("сбой на %s", key)
                await self.edit(ph, f"❌ Не скачал, {OWNER} посмотрит · {link(url)}")
            finally:
                self.busy = 0
                shutil.rmtree(work, ignore_errors=True)
                if pick and job["pick_msg"] in self.state.get("taking", []):
                    self.state["taking"].remove(job["pick_msg"])  # кнопку можно нажать снова, если сбой

    def copy_out(self, out, streamer, cut):
        d = Path(self.cfg["out_dir"])
        d.mkdir(parents=True, exist_ok=True)
        f = lambda x: tc(x).replace(":", "-")
        if "start" in cut and "end" in cut:
            lbl = f"{f(cut['start'])}_{f(cut['end'])}"
        elif "end" in cut:
            lbl = f"to_{f(cut['end'])}"
        elif cut.get("start") or cut.get("tail"):
            lbl = f"minus{cut.get('start', 0):g}s_{cut.get('tail', 0):g}s"
        else:
            lbl = "full"
        base = f"{streamer}_{datetime.now():%Y-%m-%d}_{lbl}"
        dst, i = d / f"{base}.mp4", 2
        while dst.exists():
            dst, i = d / f"{base}_{i}.mp4", i + 1
        shutil.copy2(out, dst)

    async def status_loop(self):
        while True:
            try:
                await self.set_status(True)
            except Exception:
                log.exception("статус")
            try:
                await asyncio.wait_for(self.stopping.wait(), self.cfg["status_every"])
                return
            except asyncio.TimeoutError:
                pass

    async def stop_watch(self):
        while not self.stopping.is_set():
            if STOP_FLAG.exists():
                STOP_FLAG.unlink(missing_ok=True)
                self.stopping.set()
            await asyncio.sleep(2)

    async def update_loop(self):
        """Набор: раз в UPDATE_EVERY_H проверить версию на Диске Димы; новая — скачать и перезапуститься."""
        while not self.stopping.is_set():
            try:
                if await asyncio.to_thread(download_update):
                    log.info("скачал обновление — перезапускаюсь")
                    start_detached("--apply-update")
                    self.stopping.set()
                    return
            except Exception as e:
                log.warning("проверка обновлений не вышла: %s", e)
            try:
                await asyncio.wait_for(self.stopping.wait(), UPDATE_EVERY_H * 3600)
                return
            except asyncio.TimeoutError:
                pass

    async def main(self):
        from telethon import events
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        await self.client.start(bot_token=token)
        self.group = await self.client.get_entity(self.cfg["group_id"])
        self.peer = await self.client.get_input_entity(self.group)
        self.pick_topic = self.done_topic = None
        try:
            await self.ensure_topics()
        except Exception:
            log.exception("темы не завёл — «Выбор клипов» не работает (включены ли темы и право у бота?)")
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))  # свои — нет
        self.client.add_event_handler(self.on_button, events.CallbackQuery())
        jobs = [self.worker(), self.pick_worker(), self.status_loop(), self.stop_watch()]
        if KIT:
            jobs.append(self.update_loop())
            if self.state.get("version") != KIT["version"]:
                if self.state.get("version"):
                    await self.tell_owner(f"Бот обновился до версии {KIT['version']}")
                self.state["version"] = KIT["version"]
                self.save()
        tasks = [asyncio.create_task(t) for t in jobs]
        log.info("бот запущен, группа %s%s", self.cfg["group_id"], f", версия {KIT['version']}" if KIT else "")
        await self.stopping.wait()
        log.info("выключаюсь")
        for t in tasks:
            t.cancel()
        try:
            await self.set_status(False)
        finally:
            await self.client.disconnect()


# ---------- обновления набора (только с kit.json) ----------

UPDATE_EVERY_H = 6
UPDATE_ZIP = DATA / "update.zip"


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def fetch_bytes(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_update():
    """Есть версия новее → скачать архив в data/update.zip, → True. Архив: app/… (bot.py обязательно)."""
    import zipfile
    if not KIT.get("version_url") or not KIT.get("zip_url"):
        return False
    remote = fetch_bytes(KIT["version_url"], 30).decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[\d.]+", remote) or vtuple(remote) <= vtuple(KIT["version"]):
        return False
    log.info("есть версия %s (у меня %s)", remote, KIT["version"])
    data = fetch_bytes(KIT["zip_url"], 300)
    UPDATE_ZIP.write_bytes(data)
    with zipfile.ZipFile(UPDATE_ZIP) as z:
        names = z.namelist()
        if "app/bot.py" not in names or "app/kit.json" not in names or z.testzip():
            UPDATE_ZIP.unlink(missing_ok=True)
            raise ValueError("архив обновления битый")
    return True


def start_detached(*args):
    """Запустить бота отдельным процессом без окна (pythonw), этот может спокойно выйти."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), "-I", str(HERE / "bot.py"), *args], cwd=str(ROOT),
                     creationflags=0x00000008 | 0x00000200 | NO_WINDOW, close_fds=True)  # DETACHED, NEW_GROUP


def apply_update():
    """--apply-update: дождаться выхода старого бота, распаковать data/update.zip поверх app/, запустить."""
    import zipfile
    for _ in range(60):  # старый бот держит bot.lock, пока не вышел
        try:
            with open(LOCK, "w") as f:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            break
        except OSError:
            time.sleep(1)
    if UPDATE_ZIP.exists():
        with zipfile.ZipFile(UPDATE_ZIP) as z:
            for name in z.namelist():
                if name.startswith("app/") and not name.endswith("/"):
                    dst = ROOT / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(z.read(name))
        UPDATE_ZIP.unlink(missing_ok=True)
    start_detached()


# ---------- запуск ----------

def get_updates(token):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30) as r:
        return json.load(r)["result"]


def setup():
    """Найти группу через Bot API (бот должен быть в группе, и туда нужно что-нибудь написать)."""
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    upd = get_updates(token)
    chats = {}
    for u in upd:
        m = u.get("message") or u.get("my_chat_member") or {}
        ch = m.get("chat", {})
        if ch.get("type") in ("group", "supergroup"):
            chats[ch["id"]] = ch.get("title")
        if (u.get("message") or {}).get("migrate_to_chat_id"):
            chats[u["message"]["migrate_to_chat_id"]] = ch.get("title") + " (стала супергруппой)"
    if not chats:
        print("Групп не видно: добавь бота в группу и напиши туда любое сообщение, потом повтори.")
        return
    for cid, title in chats.items():
        print(f"{cid}  {title}")
    if len(chats) == 1:
        c = load_config()
        c["group_id"] = next(iter(chats))
        save_json(CONFIG, c)
        print("group_id сохранён в config.json")
    else:
        print("Групп несколько — впиши нужный group_id в config.json сам.")


def wizard():
    """--wizard (набор): токен, ключи, группа и владелец — по шагам в консоли."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    DATA.mkdir(parents=True, exist_ok=True)
    print("\n=== Настройка бота-качалки ===\n")
    while True:
        token = input("1) Токен бота от @BotFather (вида 123456:ABC...): ").strip()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=30) as r:
                me = json.load(r)["result"]
            break
        except Exception:
            print("   Токен не подошёл — скопируй его из @BotFather целиком и попробуй ещё раз.\n")
    TOKEN_FILE.write_text(token, encoding="utf-8")
    print(f"   ок, бот @{me['username']}\n")
    print("2) Ключи Telegram: зайди на https://my.telegram.org → API development tools →")
    print("   создай приложение (любые название и адрес) → скопируй App api_id и App api_hash.")
    while True:
        api_id = input("   api_id (цифры): ").strip()
        api_hash = input("   api_hash (32 знака): ").strip()
        if api_id.isdigit() and re.fullmatch(r"[0-9a-f]{32}", api_hash):
            break
        print("   Не похоже на ключи — проверь и введи ещё раз.\n")
    save_json(TG_CONFIG, {"api_id": int(api_id), "api_hash": api_hash})
    print("\n3) Группа: создай группу в Telegram, добавь туда бота, сделай его админом")
    print("   (удаление сообщений, закрепление, управление темами), в настройках группы включи «Темы».")
    print("   Потом напиши в группу любое сообщение от себя.")
    while True:
        input("   Готово? Нажми Enter... ")
        found = None
        for u in reversed(get_updates(token)):
            m = u.get("message") or {}
            if m.get("chat", {}).get("type") == "supergroup" and m.get("from") and not m["from"].get("is_bot"):
                found = m
                break
        if found:
            break
        print("   Сообщения не видно: бот в группе и админ? Включены «Темы»? Напиши ещё раз и нажми Enter.")
    c = load_config()
    c.update({"group_id": found["chat"]["id"], "owner_id": found["from"]["id"],
              "owner_name": found["from"].get("first_name") or "владелец"})
    save_json(CONFIG, c)
    print(f"\n   ок: группа «{found['chat'].get('title')}», владелец — {c['owner_name']}")
    print("\nНастройка готова. Бот запустится сам и будет запускаться при входе в Windows.")


def main():
    global OWNER
    if "--setup" in sys.argv:
        setup()
        return
    if "--wizard" in sys.argv:
        wizard()
        return
    DATA.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG, encoding="utf-8")]
    if sys.stderr:  # под pythonw консоли нет
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if "--apply-update" in sys.argv:
        apply_update()
        return
    if "--update" in sys.argv and KIT:  # «Обновить бота.bat»: обновить сейчас, если есть что
        print("Есть новая версия — обновляю" if download_update() else "Уже последняя версия")
        if UPDATE_ZIP.exists():
            STOP_FLAG.touch()  # работающий бот выйдет сам, --apply-update дождётся
            start_detached("--apply-update")
        return
    lock = open(LOCK, "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("бот уже запущен — второй не нужен")
        return
    cfg = load_config()
    OWNER = cfg["owner_name"]
    if not cfg["group_id"]:
        log.error("нет group_id — сначала настройка (--wizard в наборе, --setup у Димы)")
        return
    STOP_FLAG.unlink(missing_ok=True)
    tmp = Path(cfg["tmp_dir"])
    shutil.rmtree(tmp, ignore_errors=True)  # остатки после сбоя
    tmp.mkdir(parents=True, exist_ok=True)
    update_ytdlp()
    asyncio.run(Bot(cfg).main())


if __name__ == "__main__":
    main()
