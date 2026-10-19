"""Досылает редакционные визуалы только для тем, у которых картинки ещё нет.

Скрипт идемпотентен: перед отправкой сверяется с очередью по тексту сцены,
поэтому повторный запуск не плодит дубли и не тратит квоту генератора.
"""
import os
import sqlite3
import sys

import httpx
from decouple import Config, RepositoryEnv

sys.path.insert(0, "/opt/content-factory/src")
from content_factory.agents.editorial import load_ideas

config = Config(RepositoryEnv("/opt/content-factory/.env"))
api = config("FOTOGEN_API_URL")
queue_db = config("FOTOGEN_QUEUE_DB")
chat = config("FOTOGEN_CHAT_ID", "0")
headers = {"x-agent-token": config("FOTOGEN_API_TOKEN")}

# Планировщик VK запускается с WorkingDirectory=/opt/content-factory-vk,
# поэтому относительный путь к кадрам разрешается именно там. Класть
# картинки в соседнее дерево бесполезно: пост останется visual_pending.
ROOT = "/opt/content-factory-vk"
assets = f"{ROOT}/assets/generated/editorial"
ideas, _ = load_ideas(f"{ROOT}/config/vk-editorial-sources.yaml")
have = {name[:-4] for name in os.listdir(assets) if name.endswith(".png")}

con = sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True)
http = httpx.Client(timeout=60)
ACTIVE = ("pending", "processing", "claimed", "running")

for idea in ideas:
    # У тем с крупной цифрой карточка рисуется локально (render_text_cards.py),
    # генератору фото их отправлять не нужно.
    if idea.id in have or idea.card_big:
        continue
    scene = idea.visual
    row = con.execute(
        "SELECT id, status, output_filename FROM jobs WHERE model=? "
        "ORDER BY id DESC LIMIT 1", (scene,)
    ).fetchone()
    if row and row[1] in ACTIVE:
        print(f"{idea.id}: уже в очереди, задание {row[0]} — пропуск")
        continue
    if row and row[1] == "done" and row[2]:
        print(f"{idea.id}: готовый кадр из задания {row[0]} — забрать вручную")
        continue
    response = http.post(
        f"{api}/api/submit-research", headers=headers,
        data={"brand": "", "model": scene, "category": "editorial", "chat_id": chat},
    )
    if response.status_code >= 400:
        print(idea.id, response.status_code, response.text[:300]); continue
    print(f"{idea.id}: отправлено, задание {response.json()['job_id']}")
