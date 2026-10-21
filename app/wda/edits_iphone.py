"""Прогон серии через НАСТОЯЩИЙ Instagram Edits на iPhone (вместо tools/edits.py).

  (сначала: python -u tools/wda/start.py — поднять связь)
  python tools/wda/edits_iphone.py 1018-1                 # серия: levelx-instagram/**/1018-1_*.mp4, порядок сборки
  python tools/wda/edits_iphone.py "ПАПКА" [файлы...]     # или папка / файлы
  python tools/wda/edits_iphone.py 1018-1 --only 1        # только первые N (проба)
  python tools/wda/edits_iphone.py 1018-1 --batch 7       # размер пачки (по умолчанию 7 — столько файлов видно в чате)
  python tools/wda/edits_iphone.py 1018-1 --phone-only    # готовое только в галерее айфона: не забирать и не чистить
  python tools/wda/edits_iphone.py 1018-1 --keep-exports  # НЕ чистить экспорты из «Фото» на iPhone (по умолчанию чистит)

Пачками по --batch креативов:
  1. лёгкие копии 1080p (частота кадров исходника, ~8 Мбит/с). Дата съёмки внутри = ЗАВТРА 0:00 + n минут:
     «Фото» и выбор в Edits сортируют по ней, так исходники всегда сверху, у каждого своя подпись
     («19 октября, 00:03»), а экспорты (сегодняшние) их не перемешают. Следующая пачка грузится в Telegram
     в фоне, пока телефон занят текущей;
  2. Telegram (бот-качалка, группа, тема «Прогон Edits» — с 19.10.2026; туда же вопросы и итог):
     Select на последних N файлах -> Share -> «Сохранить N видео», сверка по кабелю байт в байт;
  3. Edits на каждый: + -> Галерея -> видео с нужной подписью -> 2K/60 -> Далее -> «Экспортировать в 2K»
     -> «Скачать» (НЕ Instagram/Facebook);
  4. уборка: новые проекты Edits в корзину (старые не трогаются);
  5. экспорты по кабелю -> на место файлов серии; оригиналы bgswap -> tools/wda/backup/<папка>/;
  6. исходники и экспорты (уже забраны и сверены) в «Фото» удалить через «Выбрать» — с 19.10.2026 по умолчанию.
Готовые отмечаются в <папка>/.iphone-done.txt — повторный запуск их пропускает.

Нужно: iPhone по кабелю, разблокирован (автоблокировку — «Никогда»); туннель и WebDriverAgent запущены
(tools/wda/README.md).
"""
import base64
import hashlib
import http.client
import json
import io
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import place  # noqa: E402 — пути у Димы / в наборе (21.10.2026)
import wda  # noqa: E402

ROOT = place.ROOT
D = place.DATA  # всё айфонное: ответы, замены кнопок, снимки, временные копии

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

FFMPEG = place.FFMPEG
LIGHT = D / "light"
REMUX_ANY = False  # main: --phone-only / быстрая команда — не пережимать 1080p H.264 даже больше 40 МБ
# Без Telegram (20.10.2026, найдено на GitHub): файлы по кабелю в папку приложения с общим доступом к файлам
# (VLC) -> быстрая команда «Получить содержимое папки -> Сохранить в фотоальбом». Включено, если есть shortcut.json:
# {"bundle": "org.videolan.vlc-ios", "folder": "edits-in", "name": "EditsIn"}
SHORTCUT_CFG = D / "shortcut.json"


def shortcut():
    try:
        return json.loads(SHORTCUT_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
BACKUP = D / "backup"
PULLED = D / "pulled"
TELEGRAM, EDITS, PHOTOS = "ph.telegra.Telegraph", "com.burbn.basel", "com.apple.mobileslideshow"
# сделано под iPhone на русском (подписи в «Фото» — «19 октября, 00:03»); Telegram — на любом языке,
# незнакомые надписи скрипт один раз спрашивает у Димы и запоминает (aliases.json)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
BASE_W, BASE_H = 414, 896  # iPhone 11 (точки): под него сняты все координаты, на нём всё как было


class Stop(Exception):
    pass


class Missing(Stop):
    """Не нашёл свою кнопку / надпись — спросить Диму, что на экране вместо неё («где тут кнопка X»)."""

    def __init__(self, p, what):
        super().__init__(f"не нашёл: {what}")
        self.p, self.what = p, what


class Halt(Exception):
    """Дима ответил «стоп» — прогон останавливается без повторов."""


RULES = D / "auto-taps.json"  # окно с таким текстом -> какую кнопку жать (ответы Димы в Telegram)
ALIASES = D / "aliases.json"  # своя кнопка скрипта -> как она называется на этом телефоне (ответы Димы)


def load_rules():
    try:
        return json.loads(RULES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def load_aliases():
    try:
        return json.loads(ALIASES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sp(t):
    """Пробелы к одному виду: неразрывные и узкие — обычные."""
    return re.sub(r"[\xa0  ]", " ", t)


def alias_key(p):
    return re.sub(r"\d+", "#", p)  # «Сохранить 7 видео» и «Сохранить 3 видео» — одна кнопка


def alias_pred(p):
    a = load_aliases().get(alias_key(p))
    if not a or not isinstance(a, str):
        return None
    nums = re.findall(r"\d+", p)
    return f"label == {qs(a.replace('#', nums[0]) if nums else a)}"


def alias_tap(p):
    """Кнопка без надписи: Дима показал её точкой на скрине — (доля ширины, доля высоты)."""
    a = load_aliases().get(alias_key(p))
    return tuple(a["tap"]) if isinstance(a, dict) else None


def save_alias(p, label):
    """label — надпись на экране вместо нашей или {"tap": [x, y]} — точка, которую Дима нарисовал на скрине."""
    nums = re.findall(r"\d+", p)
    if isinstance(label, str) and nums and nums[0] in label:
        label = label.replace(nums[0], "#", 1)
    al = load_aliases()
    al[alias_key(p)] = label
    ALIASES.write_text(json.dumps(al, ensure_ascii=False, indent=1), encoding="utf-8")


ROUTE = {}


def route():
    """Куда идёт прогон (с 19.10.2026): бот-качалка, его группа, тема «Прогон Edits».
    Туда — лёгкие копии (без звука) и вопросы (со звуком); ответы Димы ловит bot.py и пишет в файл replies
    (у бота один читатель сообщений). Тема — config.json → edits_topic, её id — state.json → topics."""
    if not ROUTE:
        cfg = json.loads(place.BOT_CONFIG.read_text(encoding="utf-8"))
        et = cfg.get("edits_topic")
        if not et:
            sys.exit(f"в {place.BOT_CONFIG} нет edits_topic")
        topics = json.loads(place.BOT_STATE.read_text(encoding="utf-8")).get("topics", {})
        topic = topics.get(et["title"])
        if not topic:
            sys.exit(f"темы «{et['title']}» ещё нет — перезапусти бота-качалку, он создаст её сам")
        # вопросы и итоги — в отдельную тему (20.10.2026): в теме с файлами они сдвигали чат на айфоне
        ask_topic = topics.get(et.get("ask_title"), topic)
        api_id, api_hash = place.api_keys()
        chan = str(cfg["group_id"]).removeprefix("-100")
        ROUTE.update(token=place.TOKEN.read_text(encoding="utf-8").strip(),
                     chat_id=cfg["group_id"], thread=topic, ask_thread=ask_topic, title=et["title"],
                     replies=Path(et.get("replies") or D / "replies.txt"),
                     urls=[f"tg://privatepost?channel={chan}&post={topic}&thread={topic}",
                           f"tg://privatepost?channel={chan}&post={topic}"],
                     api_id=api_id, api_hash=api_hash, session=place.SEND_SESSION)
    return ROUTE


def qs(t):
    return "'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'"


def bot_api(token, method, fields=None, file=None):
    """Bot API без зависимостей (раньше — tools/tg.py; в наборе его нет). file — (поле, путь)."""
    import mimetypes
    import uuid
    url = f"https://api.telegram.org/bot{token}/{method}"
    if file is None:
        req = urllib.request.Request(url, json.dumps(fields or {}).encode(), {"Content-Type": "application/json"})
    else:
        field, path = file
        b = uuid.uuid4().hex
        crlf = "\r\n"
        parts = [f'--{b}{crlf}Content-Disposition: form-data; name="{k}"{crlf}{crlf}{v}{crlf}'.encode()
                 for k, v in (fields or {}).items()]
        mime = mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream"
        parts += [f'--{b}{crlf}Content-Disposition: form-data; name="{field}"; filename="{Path(path).name}"{crlf}'
                  f"Content-Type: {mime}{crlf}{crlf}".encode("utf-8"), Path(path).read_bytes(),
                  f"{crlf}--{b}--{crlf}".encode()]
        req = urllib.request.Request(url, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def notify(text, photo=None, sound=True):
    """Сообщение Диме в тему «Вопросы прогона» (не в тему с файлами): вопросы — со звуком, остальное — без."""
    r = route()
    f = {"chat_id": r["chat_id"], "message_thread_id": r["ask_thread"], "disable_notification": not sound}
    try:
        if photo:
            bot_api(r["token"], "sendPhoto", {**f, "caption": text[:1000],
                                             "disable_notification": str(not sound).lower()}, ("photo", photo))
        else:
            bot_api(r["token"], "sendMessage", {**f, "text": text[:4000]})
    except OSError as e:
        print(f"  не смог написать в Telegram: {e}", flush=True)


def wait_reply(since, minutes=90):
    """Ждать ответ Димы в теме: «N», «повтори», «стоп» или «точка <файл>» (скрин с точкой, куда нажать).
    Ответы пишет bot.py в файл replies — берём первый, пришедший после вопроса."""
    path = route()["replies"]
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                ts, _, ans = line.partition("\t")
                if ts.isdigit() and int(ts) >= since:
                    return ans.strip()
        except OSError:
            pass
        time.sleep(3)
    return "стоп"


def find_dot(orig, edited):
    """Где Дима нарисовал точку на скрине: сравнить со снимком, который ему ушёл. → (доля ширины, доля высоты)
    или None. Telegram пережимает фото (меньше и JPEG) — приводим к одному размеру и отсекаем шум сжатия."""
    from PIL import Image, ImageChops, ImageFilter
    a = Image.open(orig).convert("L")
    b = Image.open(edited).convert("L").resize(a.size)
    small = (a.width // 4, a.height // 4)
    d = ImageChops.difference(a.resize(small), b.resize(small)).point(lambda v: 255 if v > 70 else 0)
    d = d.filter(ImageFilter.MinFilter(3))  # одиночные точки шума — прочь, нарисованное пятно остаётся
    px = d.load()
    pts = [(x, y) for y in range(d.height) for x in range(d.width) if px[x, y]]
    if len(pts) < 3:
        return None
    return (sum(p[0] for p in pts) / len(pts) / d.width, sum(p[1] for p in pts) / len(pts) / d.height)


SIGN_WARNED = D / ".sign-warned"  # дата последнего предупреждения о подписи WDA — не чаще раза в день


def sign_check():
    """Сколько осталось подписи WDA (бесплатный Apple ID — 7 дней, по образцу SideTap doctor, 20.10.2026).
    ≤ 2 дней — раз в день сказать Диме в «Вопросы прогона»: пора переподписать в Sideloadly."""
    try:
        r = subprocess.run([sys.executable, "-m", "pymobiledevice3", "provision", "list"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        exp = [datetime.fromisoformat(p["ExpirationDate"][:19]) for p in json.loads(r.stdout)
               if "WebDriverAgent" in p.get("AppIDName", "") or "WebDriverAgent" in p.get("Name", "")]
    except Exception as e:  # noqa: BLE001
        print(f"  подпись WDA: не узнал срок ({e})", flush=True)
        return
    if not exp:
        return
    left = max(exp) - datetime.now()
    print(f"подпись WDA до {max(exp):%d.%m %H:%M} (осталось {left.days} дн)", flush=True)
    today = datetime.now().strftime("%Y-%m-%d")
    if left.days < 2 and (not SIGN_WARNED.exists() or SIGN_WARNED.read_text() != today):
        SIGN_WARNED.write_text(today)
        notify(f"🔏 Подпись WDA на айфоне кончится {max(exp):%d.%m в %H:%M}. Переподпиши в Sideloadly "
               "(tools/wda/README.md), иначе прогон встанет.")


OFFLINE = ".phone-offline"  # метка в папке серии: прогон не начался — телефон не на связи (бот: «📵 … 🔁 Повторить»)


def connect(files):
    """Связь с телефоном перед прогоном: нет — поднять start.py (сам ждёт кабель ~3 мин); не вышло — метка OFFLINE
    в папке серии, сообщение и выход с кодом 3 (20.10.2026: бот покажет «📵 Телефон не на связи» и «🔁 Повторить»)."""
    marks = {f.parent / OFFLINE for f in files}
    for m in marks:
        m.unlink(missing_ok=True)
    try:
        return Phone()
    except Exception:  # noqa: BLE001
        print("телефон не отвечает — поднимаю связь (start.py)…", flush=True)
    try:
        subprocess.run([sys.executable, "-u", str(HERE / "start.py")], capture_output=True, timeout=600)
        return Phone()
    except Exception as e:  # noqa: BLE001
        for m in marks:
            m.write_text(str(e), encoding="utf-8")
        notify("📵 Edits: телефон не на связи — подключи кабель, разблокируй айфон и запусти прогон снова "
               "(в боте — «🔁 Повторить» под пачкой).")
        sys.exit("ТЕЛЕФОН НЕ НА СВЯЗИ: подключи кабель, разблокируй и запусти снова")


def reconnect(ph):
    print("  связь с телефоном оборвалась — поднимаю заново (start.py)…", flush=True)
    subprocess.run([sys.executable, "-u", str(HERE / "start.py")], capture_output=True, timeout=600)
    ph.sid = wda.session()


def is_net(e):
    return isinstance(e, (ConnectionError, TimeoutError, http.client.HTTPException)) or (
        isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError))


def guard(ph, step, fn, again=None):
    """Шаг прогона с повтором вместо падения:
    обрыв связи -> start.py и снова; известное окно -> нажать и снова;
    иначе — скрин и кнопки Диме в бота и ждать ответ: номер кнопки (нажать и запомнить), «повтори», «стоп».
    again — что сделать перед повтором (например, перезапустить Edits)."""
    links = 0
    while True:
        try:
            return fn()
        except Halt:
            raise
        except Exception as e:  # noqa: BLE001
            if is_net(e) and links < 2:
                links += 1
                try:
                    reconnect(ph)
                except Exception as e2:  # noqa: BLE001
                    print(f"  не поднял: {e2}", flush=True)
            elif is_net(e):
                since = int(time.time())
                notify(f"⚠️ Edits на iPhone: нет связи с телефоном (шаг «{step}»).\n"
                       "Разблокируй телефон / переподключи кабель и ответь «повтори». «стоп» — остановить.")
                if wait_reply(since) == "стоп":
                    raise Halt(f"остановлено ответом «стоп» на шаге «{step}»")
                links = 0
                try:
                    reconnect(ph)
                except Exception:  # noqa: BLE001
                    pass
            elif not ph.known_popups(force=True):
                ask(ph, step, e)
            if again:
                try:
                    again()
                except Exception as e3:  # noqa: BLE001
                    print(f"  подготовка к повтору не вышла: {e3}", flush=True)


def ask(ph, step, err):
    if isinstance(err, Missing):
        return ask_where(ph, step, err)
    try:
        pic = wda.shot("ask")
        texts, btns = ph.texts(), ph.buttons()
    except Exception:  # noqa: BLE001
        pic, texts, btns = None, [], []
    lines = "\n".join(f"{i} — {b}" for i, b in enumerate(btns, 1)) or "(кнопок не вижу)"
    print(f"  СПРАШИВАЮ ДИМУ: шаг «{step}»: {err}; кнопки: {btns}", flush=True)
    ans = question(f"⚠️ Edits на iPhone застрял: {step}\n{err}\n\nКнопки на экране:\n{lines}\n\n"
                   "Ответь номером — нажму и запомню на будущее.", pic)
    if ans == "стоп":
        raise Halt(f"остановлено ответом «стоп» на шаге «{step}»")
    rule = None
    if ans.isdigit() and 1 <= int(ans) <= len(btns):
        b = btns[int(ans) - 1]
        el = ph.pred(f"type == 'XCUIElementTypeButton' AND label == {qs(b)}")
        if el:
            ph.click(el[0])
            time.sleep(1.5)
        rule = {"button": b}
    elif ans.startswith("точка "):
        pt = dot_answer(ph, pic, ans)
        if pt:
            rule = {"tap": pt}
    if rule and texts:  # запомнить: окно с этим текстом -> эта кнопка / эта точка
        rules = [r for r in load_rules() if r["text"] != texts[0]]
        rules.append({"text": texts[0], **rule, "step": step, "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
        RULES.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")


def question(text, pic):
    """Вопрос со скрином в тему «Вопросы прогона» (со звуком) и ответ Димы."""
    since = int(time.time())
    notify(text + "\n\nИли нарисуй на этом скрине точку, куда нажать, и пришли его сюда — нажму туда и запомню.\n"
           "«повтори» — если поправил сам на телефоне.\n«стоп» — остановить прогон.", Path(pic) if pic else None)
    ans = wait_reply(since)
    print(f"  ответ: {ans}", flush=True)
    return ans


def dot_answer(ph, pic, ans):
    """Скрин с точкой → нажать туда. → (доля ширины, доля высоты) или None, если точку не нашёл."""
    pt = find_dot(pic, ans.split(" ", 1)[1]) if pic else None
    if not pt:
        notify("Не нашёл точку на скрине — нарисуй крупнее и пришли ещё раз (или ответь номером).", sound=False)
        return None
    ph.tap(pt[0] * ph.W, pt[1] * ph.H)
    print(f"  нажал в точку со скрина: {pt[0]:.2f}, {pt[1]:.2f} экрана", flush=True)
    time.sleep(1.5)
    return [round(pt[0], 4), round(pt[1], 4)]


def ask_where(ph, step, err):
    """«Где тут кнопка X?» — своя кнопка скрипта не нашлась (другой язык Telegram, новая версия приложения).
    Дима отвечает номером надписи на экране или точкой на скрине — запоминаем замену (aliases.json)
    и повторяем шаг."""
    try:
        pic = wda.shot("ask")
        cand = ph.labels()
    except Exception:  # noqa: BLE001
        pic, cand = None, []
    lines = "\n".join(f"{i} — {b}" for i, b in enumerate(cand, 1)) or "(надписей не вижу)"
    print(f"  СПРАШИВАЮ ДИМУ: шаг «{step}»: где тут «{err.what}»? на экране: {cand}", flush=True)
    ans = question(f"❓ Edits на iPhone, шаг «{step}»: не нашёл «{err.what}».\n\nНа экране:\n{lines}\n\n"
                   "Ответь номером того, что это на самом деле (другой язык / новая версия) — запомню и повторю шаг.",
                   pic)
    if ans == "стоп":
        raise Halt(f"остановлено ответом «стоп» на шаге «{step}»")
    if ans.isdigit() and 1 <= int(ans) <= len(cand):
        save_alias(err.p, cand[int(ans) - 1])
        print(f"  запомнил: «{err.what}» = «{cand[int(ans) - 1]}»", flush=True)
    elif ans.startswith("точка ") and pic:
        pt = find_dot(pic, ans.split(" ", 1)[1])
        if pt:  # не жмём сейчас — шаг повторится и нажмёт сам по запомненной точке
            save_alias(err.p, {"tap": [round(pt[0], 4), round(pt[1], 4)]})
            print(f"  запомнил: «{err.what}» = точка {pt[0]:.2f}, {pt[1]:.2f} экрана", flush=True)
        else:
            notify("Не нашёл точку на скрине — нарисуй крупнее и пришли ещё раз (или ответь номером).", sound=False)


# ---------- WebDriverAgent ----------

class Phone:
    def __init__(self):
        self.sid = wda.session()
        self._popups_at = 0
        self.W, self.H = wda.window(self.sid)

    def at(self, x, y):
        """Точка, снятая на iPhone 11, — на экране этой модели (доли ширины / высоты)."""
        return x * self.W / BASE_W, y * self.H / BASE_H

    def find(self, p):
        """Элементы по условию, а нет — по замене, которую указал Дима (aliases.json)."""
        found = self.pred(p)
        if not found:
            alt = alias_pred(p)
            if alt:
                found = self.pred(alt)
        return found

    def need(self, p, what, timeout=30):
        try:
            return self.wait(lambda: self.find(p), what, timeout)
        except Missing:
            raise
        except Stop:
            raise Missing(p, what) from None

    def labels(self):
        """Видимые надписи для вопроса «где тут кнопка X» — кнопки, ячейки, тексты."""
        out = []
        for e in self.pred("(type == 'XCUIElementTypeButton' OR type == 'XCUIElementTypeCell' OR "
                           "type == 'XCUIElementTypeStaticText') AND visible == 1")[:80]:
            t = (self.label(e) or "").strip()
            if t and t not in out:
                out.append(t)
        return out[:25]

    def known_popups(self, force=False):
        """Окно, на которое Дима уже отвечал в Telegram (auto-taps.json), — нажать его кнопку самому.
        Проверка не чаще раза в 4 с, чтобы не тормозить ожидания. True — что-то нажал."""
        if not force and time.time() - self._popups_at < 4:
            return False
        self._popups_at = time.time()
        rules = load_rules()
        if not rules:
            return False
        try:
            # iOS ставит неразрывные пробелы («с\xa0геопозицией»), в стартовых ответах набора — обычные (21.10.2026)
            texts = {sp(t) for t in self.texts()}
            for r in rules:
                if sp(r["text"]) in texts and r.get("tap"):  # кнопка без надписи — точка со скрина Димы
                    self.tap(r["tap"][0] * self.W, r["tap"][1] * self.H)
                    print(f"  окно «{r['text'][:50]}» — нажал в точку со скрина", flush=True)
                    time.sleep(1)
                    return True
                if sp(r["text"]) in texts:
                    b = self.pred(f"type == 'XCUIElementTypeButton' AND label == {qs(r['button'])}")
                    if b:
                        self.click(b[0])
                        print(f"  окно «{r['text'][:50]}» — нажал «{r['button']}» (запомненный ответ)", flush=True)
                        time.sleep(1)
                        return True
        except urllib.error.HTTPError:
            pass
        return False

    def texts(self):
        """Тексты на экране, длинные — первыми (самый длинный — ключ окна в auto-taps.json)."""
        out = []
        for e in self.pred("type == 'XCUIElementTypeStaticText' AND visible == 1")[:40]:
            t = (self.label(e) or "").strip()
            if len(t) >= 8 and t not in out:
                out.append(t)
        return sorted(out, key=len, reverse=True)

    def buttons(self):
        out = []
        for e in self.pred("type == 'XCUIElementTypeButton' AND visible == 1")[:60]:
            t = (self.label(e) or "").strip()
            if t and t not in out:
                out.append(t)
        return out[:15]

    def els(self, using, value):
        return [e["ELEMENT"] for e in wda.call("POST", f"/session/{self.sid}/elements",
                                               {"using": using, "value": value})["value"]]

    def by_id(self, name):
        return self.els("accessibility id", name)

    def pred(self, p):
        return self.els("predicate string", p)

    def wait(self, finder, what, timeout=30, step=0.5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                found = finder()
                if found:
                    return found
            except urllib.error.HTTPError:
                pass
            self.known_popups()
            time.sleep(step)
        raise Stop(f"не дождался: {what}")

    def click(self, el):
        wda.call("POST", f"/session/{self.sid}/element/{el}/click")

    def press(self, name, what=None, timeout=30):
        """Найти по accessibility id и нажать; экран перерисовался (404) — найти заново."""
        self.press_pred(f"name == '{name}'", what or name, timeout)

    def press_pred(self, p, what, timeout=30, last=False):
        end = time.time() + timeout
        spot = alias_tap(p)
        if spot and not self.find(p):  # Дима показал эту кнопку точкой на скрине — у неё нет надписи
            time.sleep(1.5)            # экран успевает дорисоваться
            self.tap(spot[0] * self.W, spot[1] * self.H)
            return
        while True:
            try:
                found = self.find(p)
                if found:
                    self.click(found[-1] if last else found[0])
                    return
            except urllib.error.HTTPError:
                pass
            if time.time() > end:
                raise Missing(p, what)
            self.known_popups()
            time.sleep(0.5)

    def rect(self, el):
        return wda.call("GET", f"/session/{self.sid}/element/{el}/rect")["value"]

    def label(self, el):
        return wda.call("GET", f"/session/{self.sid}/element/{el}/attribute/label")["value"]

    def value(self, el):
        return wda.call("GET", f"/session/{self.sid}/element/{el}/attribute/value")["value"] or ""

    def name(self, el):
        return wda.call("GET", f"/session/{self.sid}/element/{el}/attribute/name")["value"]

    def tap(self, x, y):
        wda.call("POST", f"/session/{self.sid}/wda/tap", {"x": x, "y": y})

    def tap_el(self, el):
        r = self.rect(el)
        self.tap(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)

    def hold_el(self, el):
        r = self.rect(el)
        wda.call("POST", f"/session/{self.sid}/wda/touchAndHold",
                 {"x": r["x"] + r["width"] / 2, "y": r["y"] + r["height"] / 2, "duration": 1.0})

    def swipe_up(self, y0=650, y1=300, duration=0.3):
        (x, fy), (_, ty) = self.at(207, y0), self.at(207, y1)
        wda.call("POST", f"/session/{self.sid}/wda/dragfromtoforduration",
                 {"fromX": x, "fromY": fy, "toX": x, "toY": ty, "duration": duration})

    def restart(self, bundle):
        wda.call("POST", f"/session/{self.sid}/wda/apps/terminate", {"bundleId": bundle})
        wda.call("POST", f"/session/{self.sid}/wda/apps/launch", {"bundleId": bundle})

    def activate(self, bundle):
        wda.call("POST", f"/session/{self.sid}/wda/apps/activate", {"bundleId": bundle})

    def open_url(self, url):
        wda.call("POST", f"/session/{self.sid}/url", {"url": url})


# ---------- кабель (AFC) ----------

class Gallery:
    """Одно постоянное подключение к галерее iPhone (AFC) на весь прогон: ~0,01 с на запрос вместо ~3 с
    на запуск pymobiledevice3. Своё событийное кольцо в фоновом потоке; оборвалось — переподключиться."""

    def __init__(self):
        import asyncio
        self.asyncio = asyncio
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.afc = None

    def _run(self, coro):
        return self.asyncio.run_coroutine_threadsafe(coro, self.loop).result(300)

    async def _connect(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
        return AfcService(await create_using_usbmux())

    def _call(self, name, *args):
        for attempt in range(2):
            try:
                if self.afc is None:
                    self.afc = self._run(self._connect())
                f = getattr(self.afc, name)

                async def go():
                    r = f(*args)
                    return await r if self.asyncio.iscoroutine(r) else r
                return self._run(go())
            except Exception:
                self.afc = None
                if attempt:
                    raise

    def videos(self):
        """Все видео галереи, по порядку имён (IMG_0948 …)."""
        out = []
        for d in sorted(x for x in self._call("listdir", "/DCIM") if "APPLE" in x):
            out += sorted(f"/DCIM/{d}/{f}" for f in self._call("listdir", f"/DCIM/{d}")
                          if f.upper().endswith((".MP4", ".MOV")))
        return out

    def read(self, remote):
        return self._call("get_file_contents", remote)


    async def _inbox(self, bundle, fn):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.house_arrest import HouseArrestService
        ha = await HouseArrestService.create(await create_using_usbmux(), bundle, documents_only=True)
        try:
            return await fn(ha)
        finally:
            await ha.close()

    def inbox(self, bundle, fn):
        """fn(ha) — корутина над папкой Documents приложения bundle (house_arrest поверх AFC)."""
        return self._run(self._inbox(bundle, fn))


GALLERY = None


def dcim_videos():
    global GALLERY
    GALLERY = GALLERY or Gallery()
    return GALLERY.videos()


def pull(remote):
    global GALLERY
    GALLERY = GALLERY or Gallery()
    PULLED.mkdir(parents=True, exist_ok=True)
    out = PULLED / remote.split("/")[-1]
    out.write_bytes(GALLERY.read(remote))
    return out


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(p, entries):
    r = subprocess.run([place.FFPROBE, "-v", "error", "-select_streams", "v", "-show_entries", entries,
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return r.stdout.strip()


def is_real_edits(p):
    """Экспорт Edits: подпись com.burbn.basel и 1440x2560."""
    return b"com.burbn.basel" in Path(p).read_bytes()[-200000:] and probe(p, "stream=width,height") == "1440,2560"


def duration(p):
    r = subprocess.run([place.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


# ---------- пачка ----------

class Item:
    def __init__(self, src, stamp):
        self.src = src
        self.stamp = stamp                     # дата съёмки внутри лёгкой копии (локальное время)
        self.light = LIGHT / src.name
        self.export = None                     # путь экспорта в галерее iPhone
        self.export_when = None                # время экспорта (подпись в «Фото») — для чистки

    @property
    def caption(self):
        """Хвост подписи в «Фото» / выборе Edits: «19 октября, 00:03» (день без нуля, часы — с нулём)."""
        return caption_of(self.stamp)


def cells_at(ph, when):
    """Видео в «Фото» / выборе Edits, снятые в when (день + часы:минуты), — на ЛЮБОМ языке iOS (21.10.2026, для набора):
    подпись «Видео, 17 секунд, 21 октября, 00:03» / «Video, 17 seconds, October 21, 12:03 AM» / «21. Oktober, 00:03».
    Ищем по минуте (WDA), затем в двух последних частях подписи — число дня и время в 24- или 12-часовом виде."""
    mm = f"{when.minute:02d}"
    hours = {f"{when.hour:02d}", str(when.hour), str(when.hour % 12 or 12)}
    tm = re.compile(r"(?<!\d)(" + "|".join(sorted(hours)) + r"):" + mm + r"(?!\d)")
    day = re.compile(rf"(?<!\d){when.day}(?!\d)")
    out = []
    for c in ph.pred(f"name == 'PXGGridLayout-Info' AND label CONTAINS ':{mm}'"):
        tail = ", ".join((ph.label(c) or "").split(", ")[-2:])
        if tm.search(tail) and day.search(tm.sub("", tail)):
            out.append(c)
    return out


def caption_of(s):
    return f"{s.day} {MONTHS[s.month - 1]}, {s.hour:02d}:{s.minute:02d}"  # iOS: «1 ноября», день без нуля


def created(p):
    """Дата съёмки внутри файла (экспорт Edits: момент экспорта, UTC) -> местное время."""
    r = subprocess.run([place.FFPROBE, "-v", "error", "-show_entries", "format_tags=creation_time", "-of", "csv=p=0",
                        str(p)], capture_output=True, text=True).stdout.strip()
    try:
        return datetime.fromisoformat(r[:19]).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return None


SENT = ".tg-sent.json"  # в папке серии: какие лёгкие копии уже залиты в тему — {имя: {stamp, md5}}


def sent_log(d):
    try:
        return json.loads((d / SENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def make_lights(srcs, start_minute):
    """Лёгкие копии; дата съёмки — завтра 0:00 + номер, у каждой своя минута.
    Копия, уже залитая в тему прошлым (остановленным) прогоном и целая на диске, берётся как есть — it.sent,
    upload её второй раз не шлёт (20.10.2026); новым копиям — минуты, не занятые старыми (подписи не совпадут)."""
    LIGHT.mkdir(exist_ok=True)
    base = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    items, used, m = [], set(), start_minute
    for d in {s.parent for s in srcs}:
        used |= {v["stamp"] for v in sent_log(d).values()}
    for src in srcs:
        old = sent_log(src.parent).get(src.name)
        if old and (LIGHT / src.name).exists() and md5(LIGHT / src.name) == old["md5"]:
            it = Item(src, datetime.fromisoformat(old["stamp"]))
            it.sent = True
            items.append(it)
            continue
        while (base + timedelta(minutes=m)).isoformat() in used:
            m += 1
        it = Item(src, base + timedelta(minutes=m))
        m += 1
        utc = it.stamp.astimezone(timezone.utc)
        stamp = ["-map_metadata", "-1", "-metadata", f"creation_time={utc:%Y-%m-%dT%H:%M:%S}.000000Z",
                 "-movflags", "+faststart", str(it.light)]
        if (probe(src, "stream=codec_name,width,height") in ("h264,1080,1920", "h264,1920,1080")
                and (REMUX_ANY or src.stat().st_size < 40 * 2**20)):
            # уже 1080p H.264 — не пережимать, только новая дата съёмки. --phone-only («Отобранные», клипы бота
            # и так 1080p H.264): любого размера — пережатие пятёрки шло 7 минут (20.10.2026)
            subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *stamp], check=True)
            items.append(it)
            continue
        subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        # вписать в 1080×1920 / 1920×1080 с сохранением пропорций: горизонтальный клип из
                        # «Отобранных» не сплющивать (20.10.2026); 9:16 — как раньше, ровно 1080×1920
                        "-vf", "scale='if(gt(iw,ih),1920,1080)':'if(gt(iw,ih),1080,1920)'"
                               ":force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos",
                        "-c:v", "h264_nvenc", "-preset", "p5",
                        "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M", "-c:a", "aac", "-b:a", "128k",
                        "-map_metadata", "-1", "-metadata", f"creation_time={utc:%Y-%m-%dT%H:%M:%S}.000000Z",
                        "-movflags", "+faststart", str(it.light)], check=True)
        items.append(it)
    return items


def upload(items, cfg):
    """В тему «Прогон Edits», без звука; уже залитые прошлым прогоном (it.sent) — пропустить, залитые — записать."""
    new = [it for it in items if not getattr(it, "sent", False)]
    if len(new) < len(items):
        print(f"  уже в теме с прошлого запуска: {len(items) - len(new)} — не заливаю повторно", flush=True)
    if not new:
        return
    if place.KIT:  # в наборе — только по кабелю + быстрая команда (shortcut.json кладёт установщик)
        sys.exit(f"нет {SHORTCUT_CFG} — поставь быструю команду EditsIn (INSTALL-AI.md)")
    sys.path.insert(0, str(place.ROOT / "tools"))
    import tg
    tg.send([it.light for it in new], cfg)
    for d in {it.src.parent for it in new}:
        log = sent_log(d)
        for it in new:
            if it.src.parent == d:
                log[it.src.name] = {"stamp": it.stamp.isoformat(), "md5": md5(it.light)}
                it.sent = True
        (d / SENT).write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


def icon_white(img, x, y, k):
    """Доля белого в значке файла: «документ» (скачан) ~0.31, стрелка «скачать» ~0.07.
    k — пикселей снимка на точку (2 у iPhone 11, 3 у большинства других моделей)."""
    x, y, h = int(x * k), int(y * k), int(15 * k)
    box = img.crop((x - h, y - h, x + h, y + h)).convert("RGB")
    px = list(box.get_flattened_data()) if hasattr(box, "get_flattened_data") else list(box.getdata())
    return sum(1 for r, g, b in px if r > 220 and g > 220 and b > 220) / len(px)


def close_viewer(ph):
    """Открылся просмотр файла (значит, файл уже скачан) — закрыть. True, если был открыт."""
    done = ph.by_id("QLOverlayDoneButtonAccessibilityIdentifier")
    if not done:
        ph.tap(*ph.at(207, 300))         # кнопки просмотра могли спрятаться
        time.sleep(0.5)
        done = ph.by_id("QLOverlayDoneButtonAccessibilityIdentifier")
    if done:
        ph.click(done[0])
        time.sleep(1)
        return True
    return False


def viewer_open(ph):
    return bool(ph.pred("label ENDSWITH 'Меню действий'") or ph.by_id("QLOverlayDoneButtonAccessibilityIdentifier"))


def ensure_downloaded(ph, files, sizes=None):
    """Telegram сам докачивает не все файлы — нажать синий кружок на тех, что со стрелкой, и дождаться.
    Нажатие по уже скачанному открывает просмотр — тогда закрыть его и считать файл готовым.
    Не готов дольше 40 с (загрузка зависла) — крестик и снова кружок; большой файл (sizes, байт) — дольше:
    +1 с на МБ (клипы «Отобранных» без пережатия до 60 МБ, 20.10.2026)."""
    from PIL import Image
    centers = []
    for f in files:
        r = ph.rect(f)
        # круглый значок слева в пузыре: в личке пузырь с x=3, значок на 43; в группе левее ещё аватар —
        # поэтому от края пузыря, а не от края экрана (отступ в точках у всех моделей один)
        centers.append((r["x"] + 40, r["y"] + r["height"] / 2))
    shot = lambda: Image.open(io.BytesIO(base64.b64decode(wda.call("GET", "/screenshot")["value"])))
    ready = set()
    img = shot()
    k = img.width / ph.W

    def tap(i):
        ph.tap(*centers[i])
        time.sleep(1.2)
        if viewer_open(ph):
            close_viewer(ph)
            ready.add(i)

    for i, (x, y) in enumerate(centers):
        if icon_white(img, x, y, k) < 0.2:
            tap(i)
    started = {i: time.time() for i in range(len(centers))}
    limit = [40 + (s or 0) / 2**20 for s in (sizes or [0] * len(centers))]
    end = time.time() + max(300, sum(limit))
    while time.time() < end:
        img = shot()
        for i, (x, y) in enumerate(centers):
            if icon_white(img, x, y, k) > 0.25:
                ready.add(i)
        waiting = [i for i in range(len(centers)) if i not in ready]
        if not waiting:
            return
        for i in waiting:
            if time.time() - started[i] > limit[i]:
                tap(i)                   # крестик — отменить (или просмотр, если уже скачан)
                if i not in ready:
                    tap(i)               # синий кружок — скачать заново
                started[i] = time.time()
        time.sleep(1.5)
    raise Stop("Telegram: файлы не скачались на телефон")


def open_bot_chat(ph):
    """Тема «Прогон Edits» в группе бота-качалки (с 19.10.2026; раньше — личный чат с ботом LevelX).
    Только что запущенный Telegram может проглотить ссылку (19.10.2026) — тогда ещё раз, другим видом ссылки."""
    has_files = lambda: len(ph.find(FILE)) >= 1
    for url in route()["urls"] * 2:
        ph.open_url(url)
        try:
            return ph.wait(has_files, "файлы в теме «Прогон Edits»", timeout=10)
        except Stop:
            time.sleep(1)
    return ph.need(FILE, "файл в теме «Прогон Edits» (у Telegram на английском — «File»)", timeout=10)


# файл в чате Telegram (английский интерфейс; на другом языке — замена из aliases.json). В теме группы подпись другая:
# «Reply to message from: <бот> . File, from: <бот>» (20.10.2026)
FILE = "label == 'File' OR label CONTAINS ' File, from: ' OR label BEGINSWITH 'File, from: '"


def save_batch_from_telegram(ph, items):
    n = len(items)
    before = dcim_videos()
    # прошлый запуск упал уже после сохранения (например, на запросе «Разрешить») — второй раз не сохранять,
    # иначе в галерее будут дубли с теми же подписями и выбор в Edits сломается
    last = sorted(before, key=lambda f: int(re.sub(r"\D", "", f.split("/")[-1]) or 0))[-n:]
    if len(last) == n:
        got = set()
        for remote in last:
            pth = pull(remote)
            got.add(md5(pth))
            pth.unlink()
        if got == {md5(it.light) for it in items}:
            print("  эта пачка уже в галерее — пропускаю сохранение", flush=True)
            return
    ph.restart(TELEGRAM)
    time.sleep(2)
    open_bot_chat(ph)
    time.sleep(1.5)
    # Telegram открывает чат на первом непрочитанном — прокрутить в самый низ
    for _ in range(5):
        down = ph.find("label == 'перейти вниз'")
        if not down:
            break
        ph.click(down[0])
        time.sleep(1.2)
    # листать только пока не виден последний файл пачки, коротко и медленно: в теме группы рывок вниз у самого
    # низа открывает СЛЕДУЮЩУЮ тему (20.10.2026) — тогда вернуться в свою по ссылке
    last = items[-1].light.name
    # самый НИЖНИЙ файл — последний нашей пачки: после повторной заливки выше могут висеть его старые копии (20.10.2026)

    def seen():
        fs = sorted(ph.find(FILE), key=lambda e: ph.rect(e)["y"])
        return bool(fs) and (ph.value(fs[-1]) or "").startswith(last) and             ph.rect(fs[-1])["y"] + ph.rect(fs[-1])["height"] < ph.H - 60
    for _ in range(6):
        if not ph.find(f"label == {qs(route()['title'])}"):
            print("  Telegram ушёл в другую тему — возвращаюсь", flush=True)
            open_bot_chat(ph)
            time.sleep(1.5)
        if seen():
            break
        ph.swipe_up(600, 400, 0.6)
        time.sleep(1)
    time.sleep(1)
    files = sorted(ph.need(FILE, "файл в чате с ботом (у Telegram на английском — «File»)"),
                   key=lambda e: ph.rect(e)["y"])
    if len(files) < n:
        raise Stop(f"в чате видно {len(files)} файлов от бота, нужно {n}")
    # в value файла — «<имя>. Size: …» (на другом языке Telegram хвост другой, имя — то же)
    names = [ph.value(f) for f in files[-n:]]
    if not all(v.startswith(it.light.name) for v, it in zip(names, items)):
        raise Stop(f"последние файлы в чате не наши: {names}")
    ensure_downloaded(ph, files[-n:], [it.light.stat().st_size for it in items])
    ph.hold_el(files[-1])
    ph.press_pred("label == 'Select'", "«Select» в меню файла")
    time.sleep(1)
    files = sorted(ph.find(FILE), key=lambda e: ph.rect(e)["y"])
    for f in files[-n:-1]:               # нижний уже отмечен
        r = ph.rect(f)
        ph.tap(20, r["y"] + r["height"] / 2)
        time.sleep(0.3)
    chosen = f"label == '{n} Selected'"
    # в теме группы счётчика «N Selected» нет — у отмеченного файла value становится «Selected» (20.10.2026)
    marked = lambda: sum((ph.value(f) or "").startswith("Selected") for f in ph.find(FILE))
    if not ph.find(chosen) and marked() != n:
        if ph.find(FILE) and len(ph.find(FILE)) >= n and not alias_pred(chosen):
            raise Missing(chosen, f"надпись «{n} Selected» (сколько файлов отмечено)")
        raise Stop(f"Telegram: отметилось не {n} файлов")
    ph.press("Share", "кнопка «Share» внизу")
    ph.press_pred(f"label == 'Сохранить {n} видео'" if n > 1 else "label == 'Сохранить видео'",
                  "«Сохранить N видео»", timeout=60)
    # сверка: новые видео в галерее = отправленные файлы
    want = {md5(it.light) for it in items}
    end = time.time() + 120
    while time.time() < end:
        try:  # iOS время от времени сбрасывает Telegram право «добавление в Фото» и спрашивает снова
            ok = ph.find("type == 'XCUIElementTypeButton' AND label == 'Разрешить'")
            if ok:
                ph.click(ok[0])
                print("  iOS спросил разрешение на «Фото» — нажал «Разрешить»", flush=True)
        except urllib.error.HTTPError:
            pass
        new = [f for f in dcim_videos() if f not in before]
        if len(new) >= n:
            got = set()
            for remote in new:
                p = pull(remote)
                got.add(md5(p))
                p.unlink()
            if got == want:
                return
            raise Stop("в галерею попали не те файлы")
        time.sleep(3)
    raise Stop("видео из Telegram не появились в галерее")


def save_batch_via_shortcut(ph, items):
    """Пачка в «Фото» без Telegram: лёгкие копии по кабелю в Documents/<folder> приложения (VLC), быстрая команда
    сохраняет всё из папки в фотоальбом, сверка байт в байт как у Telegram, потом папка чистится."""
    global GALLERY
    GALLERY = GALLERY or Gallery()
    sc = shortcut()
    folder = "/Documents/" + sc["folder"]
    n = len(items)
    before = dcim_videos()
    want = {md5(it.light) for it in items}
    last = sorted(before, key=lambda f: int(re.sub(r"\D", "", f.split("/")[-1]) or 0))[-n:]
    if len(last) == n:  # прошлый запуск уже сохранил эту пачку — второй раз не надо (дубли подписей)
        got = set()
        for remote in last:
            pth = pull(remote)
            got.add(md5(pth))
            pth.unlink()
        if got == want:
            print("  эта пачка уже в галерее — пропускаю сохранение", flush=True)
            return

    async def fill(ha):
        if await ha.exists(folder):
            for f in await ha.listdir(folder):
                await ha.rm(f"{folder}/{f}")
        else:
            await ha.makedirs(folder)
        for it in items:
            await ha.set_file_contents(f"{folder}/{it.light.name}", it.light.read_bytes())
    GALLERY.inbox(sc["bundle"], fill)
    ph.open_url("shortcuts://run-shortcut?name=" + urllib.parse.quote(sc["name"]))
    # 60 МБ сохраняется секунды; запас — на запрос разрешения при первом запуске
    end = time.time() + 60 + sum(it.light.stat().st_size for it in items) / 2**20
    while time.time() < end:
        new = [f for f in dcim_videos() if f not in before]
        if len(new) >= n:
            got = set()
            for remote in new:
                q = pull(remote)
                got.add(md5(q))
                q.unlink()
            if got != want:
                raise Stop("в галерею попали не те файлы")

            async def clean(ha):
                for f in await ha.listdir(folder):
                    await ha.rm(f"{folder}/{f}")
            GALLERY.inbox(sc["bundle"], clean)
            return
        time.sleep(2)
    raise Stop(f"быстрая команда «{sc['name']}» не сохранила видео в «Фото»")


def export_one(ph, it, known):
    before = known
    ph.press("new-project-button", "плюс")
    ph.press("menu-item-Галерея", "меню «Галерея»")
    for _ in range(4):
        try:
            cell = ph.wait(lambda: cells_at(ph, it.stamp), f"видео «{it.caption}»", timeout=6)
            break
        except Stop:
            ph.swipe_up()
    else:
        raise Stop(f"в галерее нет видео с подписью «{it.caption}»")
    ph.tap_el(cell[0])
    time.sleep(0.6)
    ph.press_pred("type == 'XCUIElementTypeButton' AND label == 'Готово'", "«Готово»")
    ph.wait(lambda: ph.by_id("project_navigation_video_quality_button"), "редактор проекта", timeout=60)
    time.sleep(1.5)
    ph.press("project_navigation_video_quality_button", "кнопка качества")
    ph.press("video_quality_segment_1_2K", "2K")
    time.sleep(0.4)
    ph.press("video_quality_segment_2_60", "60 кадров")
    time.sleep(0.8)
    change = ph.find("type == 'XCUIElementTypeButton' AND label == 'Изменить'")
    if change:                           # исходник 25/30 fps: «Изменить частоту кадров?»
        ph.click(change[0])
        time.sleep(0.8)
    if ph.label(ph.by_id("project_navigation_video_quality_button")[0]) != "2K":
        raise Stop("не выставилось 2K")
    for _ in range(3):                   # первое «Далее» может только закрыть окно настроек
        ph.press("project_navigation_export_button", "«Далее»")
        time.sleep(1.2)
        two_k = ph.find("label == 'Экспортировать в 2K'")
        if two_k:
            ph.click(two_k[0])
            break
        if not ph.by_id("project_navigation_export_button"):
            break
    ph.wait(lambda: ph.by_id("bsl_export_download_button"), "конец экспорта", timeout=300, step=1)
    ph.press("bsl_export_download_button", "«Скачать»")
    end = time.time() + 60
    seen = False
    while time.time() < end:
        # окно, на которое Дима уже отвечал (доступ к медиатеке → «Разрешить полный доступ»), — нажать сразу,
        # а не ждать 60 с, пока шаг упадёт и guard нажмёт (20.10.2026, Дима: «раз ответ есть, чего он ждёт?»)
        if ph.known_popups():
            continue
        if not seen:  # Edits иногда предлагает доступ ко всей медиатеке — пока только записываем, что именно
            try:
                al = ph.pred("type == 'XCUIElementTypeAlert' OR label CONTAINS 'библиотек' OR label CONTAINS 'медиатек'")
                if al:
                    seen = True
                    wda.shot("library-prompt")
                    btns = [ph.label(b) for b in ph.pred("type == 'XCUIElementTypeButton'")][:12]
                    print(f"  Edits спросил про доступ к медиатеке; кнопки: {btns} (снимок shots/library-prompt.png)",
                          flush=True)
            except urllib.error.HTTPError:
                pass
        new = [f for f in dcim_videos() if f not in before]
        if new:
            it.export = new[-1]
            known.append(new[-1])
            break
        time.sleep(1)
    else:
        raise Stop("экспорт Edits не появился в галерее")
    close_share(ph)
    ph.press("project_navigation_close_button", "закрыть проект", timeout=15)
    ph.wait(lambda: ph.by_id("new-project-button"), "возврат к «Проектам»")


def close_share(ph):
    """Крестик экрана «Выберите, где поделиться» — без имени. На iPhone 11 — проверенная точка (33, 70);
    у других моделей «чёлка» другой высоты, поэтому ищем кнопку в левом верхнем углу."""
    if (ph.W, ph.H) == (BASE_W, BASE_H):
        ph.tap(33, 70)
        return
    corner = []
    for b in ph.pred("type == 'XCUIElementTypeButton' AND visible == 1")[:30]:
        r = ph.rect(b)
        if r["x"] < 80 and r["y"] < ph.H * 0.15 and r["width"] < 80:
            corner.append((r["y"], r["x"] + r["width"] / 2, r["y"] + r["height"] / 2))
    if corner:
        ph.tap(*min(corner)[1:])
    else:
        ph.tap(*ph.at(33, 70))


def trash_projects(ph, names_before):
    """Новые проекты пачки — «Выбрать» -> отметить -> «Корзина». Старые проекты не трогать."""
    fresh = [ph.name(e) for e in ph.pred("name BEGINSWITH 'project-tile-'")]
    fresh = [n for n in fresh if n not in names_before]
    if not fresh:
        return
    ph.hold_el(ph.by_id(fresh[0])[0])
    ph.press_pred("name == 'menu-item-Выбрать'", "«Выбрать» в меню проекта")
    # 19.10.2026 Edits обновился: вместо кнопки «Корзина» — нижняя панель «N проект · Переместить в корзину»
    trash_new = "type == 'XCUIElementTypeButton' AND label == 'Переместить в корзину'"
    ph.wait(lambda: ph.by_id("Корзина") or ph.find(trash_new), "режим выбора проектов")
    for name in fresh[1:]:
        ph.tap_el(ph.by_id(name)[0])
        time.sleep(0.3)
    if ph.by_id("Корзина"):
        ph.press("Корзина", "«Корзина»")
    else:
        ph.press_pred(trash_new, "«Переместить в корзину»")
        time.sleep(0.8)
    ph.press_pred("type == 'XCUIElementTypeButton' AND label BEGINSWITH 'Переместить'",
                  "подтверждение корзины", last=True)
    time.sleep(1.5)
    # плитки за краем экрана WDA не видит — сверяем только видимые: ни одна старая не должна пропасть
    left = {ph.name(e) for e in ph.pred("name BEGINSWITH 'project-tile-'")}
    gone = {n for n in names_before if n not in left}
    if gone and len(left) < len(names_before):
        raise Stop(f"после корзины пропали старые проекты: {gone}")


def delete_sources(ph, items, exports=False):
    """Исходники пачки в «Фото» — «Выбрать» -> отметить по подписям -> «Удалить».
    exports (по умолчанию, --keep-exports — выкл.) — заодно и экспорты Edits этой пачки: они уже забраны по кабелю и сверены
    (finish идёт раньше). Удалённое лежит в «Недавно удалённых» 30 дней."""
    ph.restart(PHOTOS)
    time.sleep(2.5)
    lib = ph.find("type == 'XCUIElementTypeButton' AND label == 'Медиатека'")
    if lib and not ph.find("label == 'Выбрать'"):  # «Фото» открылись на другой вкладке («Коллекции»)
        ph.click(lib[0])
        time.sleep(1.5)
    ph.press("Выбрать", "«Выбрать» в «Фото»")
    time.sleep(0.8)
    picked = 0
    for it in items:
        try:
            cells = ph.wait(lambda: cells_at(ph, it.stamp), f"исходник «{it.caption}» в «Фото»",
                            timeout=10 if picked else 15)
        except Stop:  # повтор шага: этот исходник удалили в прошлой попытке
            print(f"  исходника «{it.caption}» в «Фото» нет — уже удалён", flush=True)
            continue
        if len(cells) != 1:
            raise Stop(f"в «Фото» {len(cells)} видео с подписью «{it.caption}»")
        ph.tap_el(cells[0])
        picked += 1
        time.sleep(0.3)
    if exports:
        picked += pick_exports(ph, items)
    if not picked:
        return
    ph.press("Удалить", "«Удалить»")
    ph.press_pred("type == 'XCUIElementTypeButton' AND label BEGINSWITH 'Удалить' AND label != 'Удалить'",
                  "подтверждение удаления")
    time.sleep(1.5)
    for it in items:
        if cells_at(ph, it.stamp):
            raise Stop(f"исходник «{it.caption}» не удалился")


def pick_exports(ph, items):
    """Отметить в режиме «Выбрать» экспорты пачки по времени экспорта. Видео с той же минутой больше, чем наших
    экспортов, — не трогать эту минуту (там может быть чужое видео Димы), только сказать."""
    want, when = {}, {}
    for it in items:
        if it.export_when:
            cap = caption_of(it.export_when)
            want[cap] = want.get(cap, 0) + 1
            when[cap] = it.export_when
    picked = 0
    for cap, n in want.items():
        cells = cells_at(ph, when[cap])
        if len(cells) > n:
            print(f"  экспорты «{cap}»: в «Фото» {len(cells)} видео с этим временем, наших {n} — не трогаю", flush=True)
            continue
        for c in cells:
            ph.tap_el(c)
            picked += 1
            time.sleep(0.3)
        if len(cells) < n:
            print(f"  экспорты «{cap}»: нашёл {len(cells)} из {n} (остальные не видно на экране — останутся)", flush=True)
    return picked


def finish(items, keep=False):
    """keep (--phone-only) — экспорт остаётся только в галерее айфона: не забирать, исходник не трогать,
    только отметить в .iphone-done.txt (клипы из «Отобранных», Дима 20.10.2026)."""
    for it in items:
        done = it.src.parent / ".iphone-done.txt"
        if done.exists() and it.src.name in done.read_text(encoding="utf-8").split():
            continue  # повтор шага: этот уже забран
        if keep:
            it.light.unlink(missing_ok=True)
            with open(done, "a", encoding="utf-8") as f:
                f.write(it.src.name + "\n")
            continue
        got = pull(it.export)
        when = created(got)
        it.export_when = when
        if not is_real_edits(got):
            raise Stop(f"{it.export}: не похоже на экспорт Edits")
        if abs(duration(got) - duration(it.src)) > 0.3:
            raise Stop(f"{it.export}: длина не совпадает с {it.src.name}")
        bdir = BACKUP / it.src.parent.name
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(it.src), bdir / it.src.name)
        shutil.move(str(got), it.src)
        it.light.unlink(missing_ok=True)
        with open(it.src.parent / ".iphone-done.txt", "a", encoding="utf-8") as f:
            f.write(it.src.name + "\n")


# ---------- серия ----------

def resolve(args):
    if len(args) == 1 and not Path(args[0]).exists():
        prefix = f"{args[0]}_"
        # в порядке сборки серии (фоны у каждой серии перемешаны заново)
        return sorted((p for p in (ROOT / "levelx-instagram").rglob(f"{prefix}*.mp4")
                       if ".edits-tmp" not in p.name), key=lambda f: f.stat().st_mtime)
    files = []
    for a in args:
        p = Path(a)
        files += sorted(x for x in p.iterdir() if x.suffix.lower() == ".mp4") if p.is_dir() else [p]
    return files


def take_opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        return int(argv[i + 1]), argv[:i] + argv[i + 2:]
    return default, argv


# ---------- очередь (20.10.2026): одна серия за раз на телефон ----------
# Вторая серия, запущенная, пока идёт первая, ждёт своей очереди ДО отправки копий в тему: иначе её файлы
# упали бы в «Прогон Edits», пока айфон сохраняет оттуда чужую пачку, и новая сессия WDA сбила бы чужой прогон.
def wait_turn(series):
    """Встать в очередь (edits_queue) и ждать, пока все серии впереди закончатся (или их процесс умрёт).
    Порядок ждущих может поменять бот кнопками «⬆️ Выше / ⬇️ Ниже»; дождались — пометить «started»."""
    import atexit
    import os
    import psutil
    import edits_queue as eq
    pid = os.getpid()
    me = {"pid": pid, "created": psutil.Process(pid).create_time(), "series": series,
          "since": datetime.now().strftime("%H:%M")}
    eq.edit(lambda q: q + [me])
    atexit.register(lambda: eq.edit(lambda q: [e for e in q if e["pid"] != pid]))
    waited, told = False, None
    while True:
        q = eq.edit()
        ahead = q[:[e["pid"] for e in q].index(pid)]
        if not ahead:
            break
        if told != len(ahead):
            told = len(ahead)
            msg = (f"⏳ «{series}» в очереди: сейчас идёт «{ahead[0]['series']}»"
                   + (f", перед ней ещё {len(ahead) - 1}" if len(ahead) > 1 else "") + ". Начну сам.")
            print(msg, flush=True)
            notify(msg, sound=False)
        waited = True
        time.sleep(5)

    def start(q):
        for e in q:
            if e["pid"] == pid:
                e["started"] = True
        return q
    eq.edit(start)
    if waited:
        print(f"подошла очередь «{series}» — начинаю", flush=True)
        notify(f"▶️ Подошла очередь «{series}» — начинаю", sound=False)


def main(argv):
    only, argv = take_opt(argv, "--only")
    size, argv = take_opt(argv, "--batch", 7)
    global REMUX_ANY
    keep = "--phone-only" in argv            # готовое — только в галерее айфона, на ноутбук не забирать (20.10.2026)
    via_sc = bool(shortcut())             # без Telegram: по кабелю + быстрая команда
    REMUX_ANY = keep or via_sc
    clean = "--keep-exports" not in argv and not keep  # чистка экспортов из «Фото» — по умолчанию (Дима, 19.10.2026)
    argv = [a for a in argv if a not in ("--keep-exports", "--clean-exports", "--phone-only")]
    if not argv:
        sys.exit(__doc__)
    files = resolve(argv)
    files = [f for f in files if f.name not in ((f.parent / ".iphone-done.txt").read_text(encoding="utf-8").split()
                                               if (f.parent / ".iphone-done.txt").exists() else [])]
    if only:
        files = files[:only]
    if not files:
        sys.exit("нечего прогонять (всё уже отмечено в .iphone-done.txt?)")
    batches = [files[i:i + size] for i in range(0, len(files), size)]
    wait_turn(Path(argv[0]).name)
    ph = connect(files)
    sign_check()
    cfg = route()
    t0 = time.time()
    print(f"{len(files)} креативов, пачек: {len(batches)}")
    print("готовлю лёгкие копии пачки 1" + ("" if via_sc else " и отправляю в Telegram") + " (телефон пока не трогаю)…")
    items = make_lights(batches[0], 0)
    if not via_sc:
        upload(items, cfg)
    for b, _ in enumerate(batches):
        step = "сохранение из Telegram"
        nxt, up = None, None
        try:
            print(f"пачка {b + 1}: сохраняю в галерею " + ("быстрой командой…" if via_sc else "из Telegram…"))
            if via_sc:
                step = "сохранение быстрой командой"
                guard(ph, step, lambda: save_batch_via_shortcut(ph, items))
            else:
                guard(ph, step, lambda: save_batch_from_telegram(ph, items))
            if b + 1 < len(batches):     # следующая пачка готовится и грузится в Telegram, пока телефон занят
                nxt = []

                def prepare(srcs=batches[b + 1], start=(b + 1) * size, out=nxt):
                    out.extend(make_lights(srcs, start))
                    if not via_sc:
                        upload(out, cfg)
                up = threading.Thread(target=prepare)
                up.start()

            def to_projects():
                ph.restart(EDITS)
                ph.wait(lambda: ph.by_id("new-project-button"), "экран «Проекты» в Edits")
            step = "открыть Edits"
            guard(ph, step, to_projects)
            names_before = {ph.name(e) for e in ph.pred("name BEGINSWITH 'project-tile-'")}
            known = dcim_videos()
            for it in items:
                step = f"Edits ({it.src.name})"
                t = time.time()
                guard(ph, step, lambda: it.export or export_one(ph, it, known), again=to_projects)
                print(f"  {it.src.name}: экспорт {it.export} за {time.time() - t:.0f} с")
            print(f"пачка {b + 1}: уборка (проекты, исходники) и забор готовых файлов…")
            step = "проекты в корзину"
            guard(ph, step, lambda: trash_projects(ph, names_before), again=to_projects)
            step = "забор экспортов"         # раньше удаления: экспорт забран и сверен — тогда его можно чистить
            guard(ph, step, lambda: finish(items, keep))
            step = "удаление исходников" + (" и экспортов" if clean else "")
            guard(ph, step, lambda: delete_sources(ph, items, exports=clean))
            done = sum(len(x) for x in batches[:b + 1])
            print(f"пачка {b + 1}/{len(batches)} готова: {done}/{len(files)}, "
                  f"в среднем {(time.time() - t0) / done:.0f} с на креатив")
            if up:
                up.join()
                items = nxt
        except Exception as e:  # noqa: BLE001
            pic = None
            try:
                pic = wda.shot("error")
            except Exception:
                pass
            if up:
                up.join()
            notify(f"⛔ Прогон Edits остановлен на шаге «{step}»: {e}\nГотово {len(files) - left(files)} из "
                   f"{len(files)}. Повторный запуск продолжит с места остановки.", pic)
            sys.exit(f"СТОП на шаге «{step}»: {e}\nснимок экрана: tools/wda/shots/error.png")
    mins = (time.time() - t0) / 60
    notify(f"✅ Edits: прогнал {len(files)} креативов за {mins:.0f} мин · {argv[0]}", sound=False)
    print(f"серия прогнана за {mins:.0f} мин")


def left(files):
    """Сколько из files ещё не отмечено в .iphone-done.txt."""
    done = set()
    for d in {f.parent for f in files}:
        if (d / ".iphone-done.txt").exists():
            done |= set((d / ".iphone-done.txt").read_text(encoding="utf-8").split())
    return sum(1 for f in files if f.name not in done)


if __name__ == "__main__":
    main(sys.argv[1:])
