"""Бренд из названия, когда колонка бренда пустая (разбор 2026-11-18).

У АРУ бренд пушек AYGER есть только в названии («…AY-2000C AYGER»). С пустым
брендом фотоагент отвечает PHOTO_AUDIT_HOLD «нужны точные бренд и модель» за
секунды, а банк фото ищет по (бренд, модель) и промахивается."""
from content_factory.ingest.excel_price import infer_brand
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick

KNOWN = {"AYGER", "Rexant", "Beko", "Weissgauff"}


def test_brand_after_model_is_found():
    assert infer_brand("Тепловая пушка электрическая AY-2000C AYGER", KNOWN) == "AYGER"
    assert infer_brand("Тепловая пушка дизельная непрямого нагрева TDG-20B AYGER", KNOWN) == "AYGER"


def test_brand_case_insensitive_returns_known_spelling():
    name = ("Обогреватель электрический инфракрасный панельный 1,5кВт с кварцевыми лампами "
            "в стальном корпусе с регулировкой мощности, серый REXANT, Распродажа.")
    assert infer_brand(name, KNOWN) == "Rexant"
    assert infer_brand("Блендер погружной Weissgauff WHB-460 PSW Multi Power", KNOWN) == "Weissgauff"


def test_no_brand_inside_other_word_or_unknown():
    assert infer_brand("Пушка AYGERX-1", KNOWN) == ""
    assert infer_brand("Обогреватель 1,5 кВт", KNOWN) == ""
    assert infer_brand("", KNOWN) == ""


def test_two_different_brands_in_name_is_ambiguous():
    assert infer_brand("Переходник Beko для AYGER", KNOWN) == ""


def _fns():
    calls = {"research": []}

    def submit_research(brand, model, category, *, request_key=""):
        calls["research"].append((brand, model))
        return 101

    def read_job(job_id):
        return ("pending", None, None, None)

    def submit_card(*a, **k):
        raise AssertionError("карточка не ожидается")

    def preview(item, card_file):
        return True
    return calls, submit_research, read_job, submit_card, preview


def test_tick_fills_empty_brand_from_known_brands_before_research(tmp_path):
    s = ExcelStore(tmp_path / "s.db")
    name = "Тепловая пушка электрическая AY-2000C AYGER"
    s.select_items([("excel|aru:71472", "AYGER", "OFC510-50", "Компрессор AYGER OFC510-50", 100),
                    ("excel|aru:70777", "", name, name, 5000)])
    s.update("excel|aru:71472", status="preview")
    calls, *fns = _fns()
    tick(s, *fns)
    assert calls["research"] == [("AYGER", "AY-2000C")]
    assert s.get("excel|aru:70777").brand == "AYGER"


def test_tick_keeps_empty_brand_when_name_has_no_known_brand(tmp_path):
    s = ExcelStore(tmp_path / "s.db")
    name = "Обогреватель инфракрасный 1,5 кВт"
    s.select_items([("excel|aru:1", "", name, name, 100)])
    calls, *fns = _fns()
    tick(s, *fns)
    assert s.get("excel|aru:1").brand == ""
