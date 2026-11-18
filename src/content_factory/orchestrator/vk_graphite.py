"""Графитовые карточки фотоагента для товарных постов VK (чередование со светлыми плашками).

Владелец хочет, чтобы стена не выглядела однообразно: каждый второй товарный слот
получает фирменную графитово-золотую карточку фотоагента (те же режимы, что и у
Excel-конвейера: mcp для кондиционеров, kbt для остального), остальные — светлую
плашку завода.

Фотоагент генерирует асинхронно, от минут до часов. Ревью уходит владельцу за 48 ч
до выхода, поэтому заказ ставится только для слотов с запасом больше двух суток, а
готовая карточка подменяет плашку, лишь пока пост ещё не показан владельцу
(статус planned). Не успел агент — пост выходит со светлой плашкой.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from content_factory.orchestrator.vk_content_plan import VkContentPlanStore, slot_ordinal

TILE_SUFFIX = "_tile.jpg"
GRAPHITE_SUFFIX = "_graphite.jpg"
LEAD_SECONDS = 50 * 3600
PER_RUN = 2
MAX_PENDING = 6
MAX_TRIES = 2


@dataclass(frozen=True)
class GraphiteQueue:
    """Доступ к очереди фотоагента; submit и results подменяются в тестах."""
    submit: object          # (photo_bytes, brand, model, specs, mode) -> input_filename | None
    results: object         # (input_filenames) -> {input_filename: ("done", path) | ("failed", "")}


def enabled() -> bool:
    return os.getenv("VK_CATALOG_GRAPHITE", "0") == "1"


def is_graphite_slot(due_at: int) -> bool:
    return slot_ordinal(due_at) % 2 == 1


def _connect(store: VkContentPlanStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS vk_graphite_jobs ("
        "plan_id INTEGER PRIMARY KEY, input_filename TEXT, status TEXT NOT NULL,"
        "tries INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)")
    return connection


def _parts(caption: str) -> tuple[str, list[str]]:
    lines = [line.strip() for line in caption.splitlines() if line.strip()]
    price_at = next((i for i, line in enumerate(lines) if line.startswith("💎")), 0)
    title = lines[price_at - 1] if price_at > 0 else (lines[0] if lines else "")
    bullets = [line[1:].strip() for line in lines if line.startswith("✓")]
    return title, bullets


def _base_photo(card_path: str) -> Path:
    path = Path(card_path)
    if path.name.endswith(TILE_SUFFIX):
        return path.with_name(path.name[:-len(TILE_SUFFIX)] + ".jpg")
    return path


def sync_graphite(store: VkContentPlanStore, queue: GraphiteQueue, now: int) -> dict[str, list[int]]:
    """Забрать готовые графитовые карточки и заказать новые для будущих слотов."""
    result = {"swapped": [], "submitted": [], "failed": []}
    items = {item.id: item for item in store.list()}
    with _connect(store) as connection:
        jobs = {row[0]: row[1:] for row in connection.execute(
            "SELECT plan_id,input_filename,status,tries FROM vk_graphite_jobs")}
        pending = {plan_id: job[0] for plan_id, job in jobs.items() if job[1] == "pending"}
        answers = queue.results(list(pending.values())) if pending else {}
        for plan_id, input_filename in pending.items():
            state, output = answers.get(input_filename, ("pending", ""))
            item = items.get(plan_id)
            if state == "failed":
                connection.execute("UPDATE vk_graphite_jobs SET status='failed',updated_at=? "
                                   "WHERE plan_id=?", (now, plan_id))
                result["failed"].append(plan_id)
                continue
            if state != "done" or not output or not Path(output).is_file():
                continue
            connection.execute("UPDATE vk_graphite_jobs SET status='done',updated_at=? "
                               "WHERE plan_id=?", (now, plan_id))
            # Владелец уже видел пост — картинку не подменяем, иначе выйдет не то, что одобрено.
            if item is None or item.status != "planned":
                continue
            target = _base_photo(item.card_path).with_name(
                _base_photo(item.card_path).stem + GRAPHITE_SUFFIX)
            shutil.copyfile(output, target)
            cursor = connection.execute(
                "UPDATE vk_content_plan SET card_path=?,updated_at=? WHERE id=? AND status='planned'",
                (str(target), now, plan_id))
            if cursor.rowcount:
                result["swapped"].append(plan_id)

        budget = min(PER_RUN, MAX_PENDING - (len(pending) - len(result["failed"])))
        for item in sorted(items.values(), key=lambda i: i.due_at):
            if budget <= 0:
                break
            if not (item.source_key.startswith("catalog:") and item.status == "planned"
                    and item.due_at > now + LEAD_SECONDS and is_graphite_slot(item.due_at)
                    and str(item.card_path).endswith(TILE_SUFFIX)):
                continue
            job = jobs.get(item.id)
            if job and (job[1] in {"pending", "done"} or job[2] >= MAX_TRIES):
                continue
            photo = _base_photo(item.card_path)
            if not photo.is_file():
                continue
            title, bullets = _parts(item.caption)
            mode = "mcp" if item.category == "air_conditioners" else "kbt"
            try:
                input_filename = queue.submit(photo.read_bytes(), item.brand, title,
                                              "\n".join(bullets), mode)
            except Exception:  # noqa: BLE001 — очередь недоступна, попробуем в следующий прогон
                break
            if not input_filename:
                continue
            connection.execute(
                "INSERT INTO vk_graphite_jobs(plan_id,input_filename,status,tries,updated_at) "
                "VALUES(?,?,'pending',?,?) ON CONFLICT(plan_id) DO UPDATE SET "
                "input_filename=excluded.input_filename,status='pending',"
                "tries=excluded.tries,updated_at=excluded.updated_at",
                (item.id, input_filename, (job[2] if job else 0) + 1, now))
            result["submitted"].append(item.id)
            budget -= 1
    return result


def fotogen_queue() -> GraphiteQueue:
    """Очередь фотоагента из .env завода (FOTOGEN_*), как у Excel-конвейера."""
    from content_factory.cards_pipeline import (
        FotogenConfig, done_results, failed_inputs, submit_card_job, wake_agent,
    )
    cfg = FotogenConfig(
        api_url=os.environ["FOTOGEN_API_URL"], token=os.environ["FOTOGEN_API_TOKEN"],
        chat_id=int(os.getenv("FOTOGEN_CHAT_ID", "0") or 0),
        queue_db=os.environ["FOTOGEN_QUEUE_DB"], output_dir=os.environ["FOTOGEN_OUTPUT_DIR"],
        cards_dir="", mode="kbt")

    def submit(photo_bytes, brand, model, specs, mode):
        model_name = re.sub(r"[^\w\-]+", "_", model)[:60] or "card"
        queued = submit_card_job(cfg, photo_bytes, brand, model_name, specs, mode=mode)
        try:
            wake_agent(cfg.queue_db)
        except Exception:  # noqa: BLE001
            pass
        return queued

    def results(input_filenames):
        done = done_results(cfg.queue_db, input_filenames)
        failed = failed_inputs(cfg.queue_db, input_filenames)
        out = {name: ("done", str(Path(cfg.output_dir) / output)) for name, output in done.items()}
        out.update({name: ("failed", "") for name in failed})
        return out

    return GraphiteQueue(submit=submit, results=results)


def run_graphite(store: VkContentPlanStore, now: int | None = None) -> dict:
    if not enabled():
        return {"disabled": True}
    return sync_graphite(store, fotogen_queue(), int(now or time.time()))
