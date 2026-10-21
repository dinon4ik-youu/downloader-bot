"""Серию креативов — в тему «Кинуть в Edits» бота-качалки, под каждым кнопка «📱 Прогнать через Edits» (20.10.2026).
Дальше владелец сам жмёт кнопки (подтверждение «✅ Да») — креативы встают в общую очередь айфона, готовое бот присылает
ответом на каждый.

  python tools/wda/to_edits_topic.py "<папка серии>"     # все .mp4 папки, в порядке сборки
  python tools/wda/to_edits_topic.py файл1.mp4 файл2.mp4
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import place  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def files_of(args):
    out = []
    for a in args:
        p = Path(a)
        out += sorted((x for x in p.iterdir() if x.suffix.lower() == ".mp4"), key=lambda f: f.stat().st_mtime) \
            if p.is_dir() else [p]
    return out


def dims(p):
    r = subprocess.run([place.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "json", str(p)],
                       capture_output=True, text=True).stdout
    j = json.loads(r)
    s = j["streams"][0]
    return s["width"], s["height"], float(j["format"]["duration"])


async def main(args):
    from telethon import Button, TelegramClient
    from telethon.tl import types
    files = files_of(args)
    if not files:
        sys.exit(__doc__)
    cfg = json.loads(place.BOT_CONFIG.read_text(encoding="utf-8"))
    title = cfg["edits_topic"]["drop_title"]
    topic = json.loads(place.BOT_STATE.read_text(encoding="utf-8"))["topics"][title]
    api_id, api_hash = place.api_keys()
    client = TelegramClient(str(place.SEND_SESSION), int(api_id), api_hash)
    await client.start(bot_token=place.TOKEN.read_text(encoding="utf-8").strip())
    group = await client.get_input_entity(cfg["group_id"])
    for i, f in enumerate(files, 1):
        w, h, d = dims(f)
        await client.send_file(group, str(f), reply_to=topic, silent=True, caption=f.stem,
                               attributes=[types.DocumentAttributeVideo(duration=round(d, 2), w=w, h=h,
                                                                        supports_streaming=True)],
                               buttons=[Button.inline("📱 Прогнать через Edits", b"edits")])
        print(f"  {i}/{len(files)} {f.name}", flush=True)
    await client.disconnect()
    print(f"готово: {len(files)} шт в «{title}»")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
