"""Автогенерация карточек: мост к очереди фотоагента (ritualb2b vps_api на 127.0.0.1:8765).

Поток (на том же VPS, что и очередь):
  1) забрать готовые: задачи со status='done' → копируем output/{файл} в avito-cards/{ключ}.jpg;
  2) поставить новые (throttle per_run): для серий без карточки скачиваем фото серии и
     POST /api/submit-job (mode=conditioner, brand, model=серия, specs, chat_id).
Локальный агент (Windows+Chrome) генерит через веб-ChatGPT и кладёт результат в очередь.
Маппинг задача→серия — по input_filename (его возвращает submit-job) в нашей CardJobStore.
"""
from __future__ import annotations
import io
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import httpx

from content_factory.content.cards import card_key


@dataclass
class FotogenConfig:
    api_url: str
    token: str
    chat_id: int
    queue_db: str
    output_dir: str
    cards_dir: str
    mode: str = "conditioner"  # режим по умолчанию
    modes: dict = None         # {series_key: режим} — переопределяет mode для конкретной серии
    per_run: int = 8           # максимум новых задач за один запуск
    max_pending: int = 15      # потолок «в работе» (чтобы не гнать сотни подряд — риск ToS)
    max_total: int = 100000    # ВСЕГО карточек к генерации (для теста ставим ~20; потом снимем)


# ── очередь-API фотоагента ──────────────────────────────────────────────────
def submit_card_job(cfg: FotogenConfig, photo_bytes: bytes, brand: str, model: str,
                    specs: str, http: httpx.Client | None = None,
                    mode: str | None = None) -> str | None:
    """POST /api/submit-job → имя поставленного входного файла (для маппинга)."""
    client = http or httpx.Client(timeout=30)
    r = client.post(
        f"{cfg.api_url.rstrip('/')}/api/submit-job",
        headers={"x-agent-token": cfg.token},
        data={"mode": mode or cfg.mode, "specs": specs, "brand": brand or "",
              "model": model or "", "chat_id": str(cfg.chat_id)},
        files={"photo": (f"{(model or 'card')}.jpg".replace(" ", "_"),
                         io.BytesIO(photo_bytes), "image/jpeg")},
    )
    r.raise_for_status()
    return (r.json() or {}).get("queued")


def _query_jobs(queue_db: str, input_filenames: list[str], status: str) -> dict[str, str]:
    if not input_filenames:
        return {}
    con = sqlite3.connect(f"file:{Path(queue_db).as_posix()}?mode=ro", uri=True)
    try:
        qs = ",".join("?" * len(input_filenames))
        rows = con.execute(
            f"SELECT input_filename, output_filename FROM jobs "
            f"WHERE status=? AND input_filename IN ({qs})",
            [status, *input_filenames]).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


def wake_agent(queue_db: str) -> None:
    """Сигнал WatchDog на локальных ПК: запустить агента (он обработает очередь).
    Тот же механизм, что кнопка «🚀 Запустить агента». Бродкаст на ВСЕ ключи:
    ноут слушает только agent_command_laptop (адресные флаги 2026-07-05) —
    один глобальный ключ его не будил, очередь могла ждать агента вечно."""
    con = sqlite3.connect(queue_db, timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
        for key in ("agent_command", "agent_command_laptop", "agent_command_desktop"):
            con.execute("INSERT OR REPLACE INTO flags (key, value) VALUES (?, 'start')",
                        (key,))
        con.commit()
    finally:
        con.close()


def done_results(queue_db: str, input_filenames: list[str]) -> dict[str, str]:
    return {k: v for k, v in _query_jobs(queue_db, input_filenames, "done").items() if v}


def failed_inputs(queue_db: str, input_filenames: list[str]) -> set[str]:
    return set(_query_jobs(queue_db, input_filenames, "failed").keys())


def cancelled_inputs(queue_db: str, input_filenames: list[str]) -> set[str]:
    return set(_query_jobs(queue_db, input_filenames, "cancelled").keys())


# ── состояние (маппинг серия→задача) ────────────────────────────────────────
MAX_TRIES = 3   # сколько раз пробуем сгенерировать карточку серии при failed (потом сдаёмся)


class CardJobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS card_jobs "
                      "(key TEXT PRIMARY KEY, input_filename TEXT, status TEXT)")
            try:                       # миграция: счётчик попыток (для авто-ретрая failed)
                c.execute("ALTER TABLE card_jobs ADD COLUMN tries INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass                   # колонка уже есть

    def _c(self):
        return sqlite3.connect(self.path)

    def get(self, key: str):
        """(input_filename, status, tries) или None."""
        with self._c() as c:
            return c.execute("SELECT input_filename, status, COALESCE(tries,0) "
                             "FROM card_jobs WHERE key=?", (key,)).fetchone()

    def pending(self) -> list[tuple[str, str]]:
        with self._c() as c:
            return list(c.execute("SELECT key, input_filename FROM card_jobs WHERE status='pending'"))

    def count(self) -> int:
        with self._c() as c:
            return c.execute("SELECT count(*) FROM card_jobs WHERE status!='failed'").fetchone()[0]

    def record(self, key: str, input_filename: str, status: str, tries: int = 0) -> None:
        with self._c() as c:
            c.execute("INSERT INTO card_jobs(key,input_filename,status,tries) VALUES(?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET input_filename=excluded.input_filename, "
                      "status=excluded.status, tries=excluded.tries",
                      (key, input_filename, status, tries))


_SKIP_SPEC_KEYS = {"Бренд", "Модель", "Серия", "Модель внутреннего блока", "Модель наружного блока"}


def specs_text(attrs: dict, max_lines: int = 8) -> str:
    out = []
    for k, v in attrs.items():
        if k in _SKIP_SPEC_KEYS:
            continue
        v = (v or "").replace("( - )", "").strip()
        if v:
            out.append(f"{k}: {v}")
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _http_get(url: str) -> bytes:
    return httpx.get(url, headers={"User-Agent": "AvitoBridge/1.0"}, timeout=30).content


def run_once(groups, cfg: FotogenConfig, store: CardJobStore,
             http: httpx.Client | None = None, fetch_photo=None,
             specs_fn=None) -> tuple[int, int]:
    """Один проход. Возвращает (submitted, published).
    specs_fn(group) -> текст ТТХ для агента (если задан) — иначе specs_text(rep.attrs).
    Через specs_fn отдаём агенту те же «ключевые особенности», что и в подписи."""
    cards = Path(cfg.cards_dir)
    cards.mkdir(parents=True, exist_ok=True)
    out_dir = Path(cfg.output_dir)
    fetch_photo = fetch_photo or _http_get
    submitted = published = 0

    # 1) забрать готовые
    pend = store.pending()
    if pend:
        in2key = {f: k for k, f in pend}
        for in_fn, out_fn in done_results(cfg.queue_db, list(in2key)).items():
            src = out_dir / out_fn
            if src.exists():
                dst = cards / f"{in2key[in_fn]}.jpg"
                shutil.copyfile(src, dst)
                dst.chmod(0o644)
                store.record(in2key[in_fn], in_fn, "done")
                published += 1
        for in_fn in failed_inputs(cfg.queue_db, list(in2key)):
            key = in2key[in_fn]
            prev = store.get(key)
            store.record(key, in_fn, "failed", tries=(prev[2] if prev else 1))   # сохраняем счётчик попыток
        # снятые в очереди задачи (cancelled) не должны вечно висеть pending и съедать
        # бюджет max_pending → считаем их failed (переотправятся, пока есть попытки)
        for in_fn in cancelled_inputs(cfg.queue_db, list(in2key)):
            key = in2key[in_fn]
            prev = store.get(key)
            store.record(key, in_fn, "failed", tries=(prev[2] if prev else 1))

    # 2) поставить новые (серии без карточки), с потолком «в работе» И общим лимитом max_total
    outstanding = len(store.pending())
    total_slots = max(0, cfg.max_total - store.count())   # глобальный лимит карточек (тест ~20)
    budget = max(0, min(cfg.per_run, cfg.max_pending - outstanding, total_slots))
    for g in groups:
        if submitted >= budget:
            break
        key = card_key(g.supplier_sku)
        if (cards / f"{key}.jpg").exists():
            continue
        st = store.get(key)
        next_tries = 1
        if st:
            status, tries = st[1], st[2]
            if status in ("pending", "done"):
                continue
            if status == "failed" and tries >= MAX_TRIES:
                continue                       # исчерпали попытки — сдаёмся (не долбим агента)
            next_tries = tries + 1             # failed с запасом попыток → переотправляем
        rep = g.representative
        photo_url = rep.photos[0] if rep.photos else None
        if not photo_url:
            continue
        mode = (cfg.modes or {}).get(getattr(g, "key", None)) or cfg.mode
        specs = specs_fn(g) if specs_fn else specs_text(rep.attrs)
        try:
            in_fn = submit_card_job(cfg, fetch_photo(photo_url), g.brand, g.series,
                                    specs, http=http, mode=mode)
        except Exception:
            continue
        if in_fn:
            store.record(key, in_fn, "pending", tries=next_tries)
            submitted += 1

    # Есть незавершённые задачи → будим локального агента (WatchDog поднимет Chrome+агента).
    if store.pending():
        try:
            wake_agent(cfg.queue_db)
        except Exception:
            pass
    return submitted, published
