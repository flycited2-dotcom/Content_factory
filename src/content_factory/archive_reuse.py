"""Повторное использование уже сгенерированного вместо новых заданий ChatGPT.

Владелец (2026-10-13): «не жечь токены повторно» — товар, который уже генерировался,
не отправлять в research и на карточку заново. Порядок:

1. Готовая карточка — того же товара (совпадает полное название из прайса без учёта
   регистра и разделителей: другой цвет/комплектация — другой товар) и того же стиля
   карточки, только из ручной задачи, дошедшей до превью. Файл берём из выхода
   фотоагента или из архива по датам (cf-archive); при необходимости копируем в выход,
   откуда превью его забирает. Цена на карточке не печатается — подпись соберётся с
   текущей ценой.
2. Иначе исходное фото с характеристиками из кэша research — по бренду и коду модели
   («VT-MW0823»), если фото ещё на диске. Тогда генерируется только карточка.

Только чтение истории; позиции ready-price (Авито) сюда не относятся — у них свой архив.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

_MIN_CODE = 4


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold())


def name_key(name: str) -> str:
    """Тот же товар = то же полное название из прайса (без регистра и разделителей)."""
    return _compact(name)


def model_identity(brand: str, model: str) -> tuple[str, str] | None:
    """(бренд, код модели) для поиска исходного фото; без бренда или кода — None."""
    from content_factory.ingest.excel_price import research_model
    brand_key = (brand or "").strip().casefold()
    code = _compact(research_model(model or ""))
    if not brand_key or len(code) < _MIN_CODE or not re.search(r"\d", code):
        return None
    return brand_key, code


class ArchiveReuse:
    def __init__(self, state_db, queue_db, output_dir, archive_dir):
        self.state_db, self.queue_db = Path(state_db), Path(queue_db)
        self.output_dir, self.archive_dir = Path(output_dir), Path(archive_dir)
        self._cards = None
        self._photos = None

    # ── готовые карточки ──────────────────────────────────────────────────────
    def _card_index(self) -> dict:
        if self._cards is not None:
            return self._cards
        self._cards = {}
        with sqlite3.connect(f"file:{self.state_db.as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT key, brand, model, name, card_mode, card_job FROM excel_items "
                              "WHERE status='preview' AND card_job IS NOT NULL "
                              "AND key NOT LIKE 'ready-price|%' ORDER BY ts").fetchall()
        if not rows or not self.queue_db.is_file():
            return self._cards
        with sqlite3.connect(f"file:{self.queue_db.as_posix()}?mode=ro", uri=True) as q:
            jobs = {r[0]: r[1] for r in q.execute(
                "SELECT id, output_filename FROM jobs WHERE status='done' AND output_filename IS NOT NULL")}
        for key, brand, model, name, mode, job in rows:
            output = jobs.get(job)
            if output:                                  # поздние перекрывают ранние
                self._cards[(name_key(name), (mode or "kbt").casefold())] = (key, brand, model, output)
        return self._cards

    def _locate(self, filename: str) -> Path | None:
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return None
        direct = self.output_dir / filename
        if direct.is_file():
            return direct
        if self.archive_dir.is_dir():
            for day in sorted(self.archive_dir.iterdir(), reverse=True):
                candidate = day / filename
                if candidate.is_file():
                    return candidate
        return None

    def card_for(self, item) -> tuple[str, str] | None:
        """(имя файла в выходе фотоагента, УТП для подписи) или None."""
        if item.key.startswith("ready-price|") or not name_key(item.name):
            return None
        found = self._card_index().get((name_key(item.name), (item.card_mode or "kbt").casefold()))
        if not found or found[0] == item.key:
            return None
        _, brand, model, output = found
        utp = self._utp(brand, model)
        path = self._locate(output)
        if not utp or path is None:
            return None
        if path.parent != self.output_dir:             # из архива по датам — в выход фотоагента
            target = self.output_dir / f"reuse_{output}"
            if not target.is_file():
                tmp = target.with_suffix(target.suffix + ".tmp")
                shutil.copyfile(path, tmp)
                os.replace(tmp, target)
            output = target.name
        return output, utp

    def _utp(self, brand: str, model: str) -> str | None:
        with sqlite3.connect(f"file:{self.state_db.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT utp FROM research_cache WHERE model_key=?",
                             (f"{(brand or '').strip().lower()}|{(model or '').strip().lower()}",)).fetchone()
        return row[0] if row and row[0] else None

    # ── исходные фото из кэша research ───────────────────────────────────────
    def _photo_index(self) -> dict:
        if self._photos is not None:
            return self._photos
        self._photos = {}
        with sqlite3.connect(f"file:{self.state_db.as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT model_key, utp, photo_path FROM research_cache "
                              "WHERE photo_path IS NOT NULL AND utp IS NOT NULL ORDER BY ts").fetchall()
        for model_key, utp, photo in rows:
            brand, _, model = model_key.partition("|")
            identity = model_identity(brand, model)
            if identity and utp and photo and Path(photo).is_file():
                self._photos[identity] = (utp, photo)    # самое свежее — последним
        return self._photos

    def cached_for(self, item) -> tuple[str, str] | None:
        """(УТП, путь к исходному фото) того же бренда и кода модели или None."""
        if item.key.startswith("ready-price|"):
            return None
        identity = model_identity(item.brand, item.model)
        return self._photo_index().get(identity) if identity else None
