"""Где что лежит — у Димы (tools/wda) и в наборе для других (<набор>/app/wda, 21.10.2026).
В наборе рядом с app/bot.py лежит kit.json: настройки и секреты бота — <набор>/data, всё айфонное — <набор>/data/wda,
ffmpeg — <набор>/bin. У Димы — как было: tools/wda, tools/shared_bot, tools/tg-config.json."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = (HERE.parent / "kit.json").exists()

if KIT:
    ROOT = HERE.parent.parent
    BOT_DATA = ROOT / "data"
    DATA = BOT_DATA / "wda"
    FFMPEG = ROOT / "bin" / "ffmpeg.exe"
    TOKEN = BOT_DATA / "token.txt"
    API = BOT_DATA / "api.json"           # {"api_id": ..., "api_hash": ...}
    SEND_SESSION = BOT_DATA / "send"      # своя сессия: bot.session держит работающий бот
else:
    TOOLS = HERE.parent
    ROOT = TOOLS.parent
    BOT_DATA = TOOLS / "shared_bot"
    DATA = HERE
    FFMPEG = TOOLS / "fw" / "Faster-Whisper-XXL" / "ffmpeg.exe"
    TOKEN = TOOLS / "shared-bot-token.txt"
    API = TOOLS / "tg-config.json"
    SEND_SESSION = BOT_DATA / "send.session"

FFPROBE = str(FFMPEG.with_name("ffprobe.exe")) if FFMPEG.with_name("ffprobe.exe").exists() else "ffprobe"
BOT_CONFIG = BOT_DATA / "config.json"
BOT_STATE = BOT_DATA / "state.json"
DATA.mkdir(parents=True, exist_ok=True)
for _d in (HERE / "defaults").glob("*.json") if (HERE / "defaults").is_dir() else []:  # набор: стартовые настройки
    if not (DATA / _d.name).exists():
        (DATA / _d.name).write_bytes(_d.read_bytes())


def api_keys():
    """api_id / api_hash Telegram (для отправки больших файлов от имени бота)."""
    try:
        c = json.loads(API.read_text(encoding="utf-8"))
        return c.get("api_id"), c.get("api_hash")
    except (OSError, ValueError):
        return None, None


def owner():
    """Имя владельца в сообщениях («ответь …»): config.json → owner_name, у Димы — «Дима»."""
    try:
        return json.loads(BOT_CONFIG.read_text(encoding="utf-8")).get("owner_name") or ("владелец" if KIT else "Дима")
    except (OSError, ValueError):
        return "владелец" if KIT else "Дима"
