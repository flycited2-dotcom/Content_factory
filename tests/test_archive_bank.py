"""Банк фото до research (разбор 2026-11-18): готовая карточка того же бренда и кода
модели из очереди фотоагента — в том числе сделанная конвейером Авито (aru-avito-*),
у которого нет записи в excel_items. ChatGPT повторно не дёргаем."""
import sqlite3

from content_factory.archive_reuse import ArchiveReuse, utp_from_specs
from content_factory.orchestrator.excel_pipeline import ExcelStore

AVITO_SPECS = """Название товара: Тепловая пушка электрическая AY-2000C AYGER
Бренд: AYGER
Модель: AY-2000C
ТОЧНЫЙ ТЕКСТ ЕДИНСТВЕННОГО ПРАВОГО ИНФОРМАЦИОННОГО БЛОКА:
Мощность: 2 Квт
Производительность по воздуху: 120 м³/ч
Тип термостата: механический
Другие характеристики на изображение не добавлять. В заголовке только тип, бренд и модель, без скобок и характеристик. Подзаголовок без цифр.
ФИНАЛЬНЫЙ ФОРМАТ ДЛЯ АВИТО: горизонтальное изображение 4:3. Скомпонуй товар, логотип и все надписи в пределах этого кадра."""


def _queue(path, jobs):
    with sqlite3.connect(path) as q:
        q.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, mode TEXT, brand TEXT, model TEXT, "
                  "status TEXT, output_filename TEXT, specs TEXT)")
        q.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", jobs)


def _setup(tmp_path, jobs, name="Тепловая пушка электрическая AY-2000C AYGER"):
    store = ExcelStore(tmp_path / "state.db")
    store.select_items([("excel|aru:70777", "AYGER", "AY-2000C", name, 5000)])
    _queue(tmp_path / "queue.db", jobs)
    out, arch = tmp_path / "out", tmp_path / "archive"
    out.mkdir()
    (arch / "2026-11-13").mkdir(parents=True)
    (arch / "2026-11-13" / "ayger_ay-2000c_2026-11-13_094353.png").write_bytes(b"png")
    reuse = ArchiveReuse(tmp_path / "state.db", tmp_path / "queue.db", out, arch)
    return store.get("excel|aru:70777"), reuse, out


def test_utp_from_avito_specs_keeps_only_facts():
    assert utp_from_specs(AVITO_SPECS) == ("✓ Мощность: 2 Квт\n"
                                           "✓ Производительность по воздуху: 120 м³/ч\n"
                                           "✓ Тип термостата: механический")


def test_utp_from_factory_specs_is_kept_as_is():
    assert utp_from_specs("2 кВт\n✓ Тепловой насос\n✓ 15 программ") == "✓ Тепловой насос\n✓ 15 программ"
    assert utp_from_specs("") is None


def test_card_from_bank_by_brand_and_model_code(tmp_path):
    item, reuse, out = _setup(tmp_path, [
        (4828, "kbt", "AYGER", "AY-2000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS)])
    card, utp = reuse.card_for(item)
    assert card == "reuse_ayger_ay-2000c_2026-11-13_094353.png"
    assert (out / card).read_bytes() == b"png"
    assert utp.startswith("✓ Мощность: 2 Квт")


def test_bank_ignores_other_model_mode_and_unfinished_jobs(tmp_path):
    item, reuse, _ = _setup(tmp_path, [
        (1, "kbt", "AYGER", "AY-3000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS),
        (2, "conditioner", "AYGER", "AY-2000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS),
        (3, "kbt", "AYGER", "AY-2000C", "failed", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS)])
    assert reuse.card_for(item) is None


def test_bank_skips_missing_file_and_job_without_facts(tmp_path):
    item, reuse, _ = _setup(tmp_path, [
        (1, "kbt", "AYGER", "AY-2000C", "done", "gone.png", AVITO_SPECS),
        (2, "kbt", "AYGER", "AY-2000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", "без фактов")])
    assert reuse.card_for(item) is None


def test_research_cache_utp_has_priority_over_specs(tmp_path):
    item, reuse, _ = _setup(tmp_path, [
        (4828, "kbt", "AYGER", "AY-2000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS)])
    ExcelStore(tmp_path / "state.db").cache_put("ayger|ay-2000c", "✓ Из кэша", None)
    assert reuse.card_for(item)[1] == "✓ Из кэша"


def test_bank_needs_brand(tmp_path):
    item, reuse, _ = _setup(tmp_path, [
        (4828, "kbt", "AYGER", "AY-2000C", "done", "ayger_ay-2000c_2026-11-13_094353.png", AVITO_SPECS)])
    item.brand = ""
    assert reuse.card_for(item) is None
