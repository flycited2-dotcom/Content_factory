"""PHOTO_AUDIT_HOLD в алертах Никиты (разбор 2026-11-18): три вида остановки фото
фотоагентом. Раньше алерт писал «ошибка этапа … Повтор запустит этап заново» — повтор
падал за секунды тем же отказом. Теперь — понятная причина, а где повтор бесполезен —
вместо «Искать заново» предложение прислать фото ответом на сообщение."""
import json

from content_factory.bot.manual_photo import make_manual_photo_fn, preview_code_from_reply
from content_factory.bot.run import make_regen_fn
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.orchestrator.excel_run import describe_failure, failure_markup
from content_factory.publish.orders import OrderLinks

BRAND = ("research: PHOTO_AUDIT_HOLD: Для товарной фотографии нужны точные бренд и модель; "
         "требуется ручной разбор")
SOURCE = ("research: PHOTO_AUDIT_HOLD: SOURCE_UNAVAILABLE: три источника не дали "
          "проверяемого фото точной модели")
UNCONFIRMED = ("card: PHOTO_AUDIT_HOLD: Не подтверждён результат начатой генерации; "
               "требуется сверить сохранённый файл")


def test_brand_model_hold_is_explained_without_useless_retry():
    cause, advice, _ = describe_failure(BRAND)
    assert "бренд" in cause and "модел" in cause
    assert "Повтор запустит" not in advice
    assert "фото" in advice and "ответом" in advice


def test_source_unavailable_hold_offers_photo():
    cause, advice, _ = describe_failure(SOURCE)
    assert "источник" in cause
    assert "ответом" in advice


def test_unconfirmed_generation_hold_keeps_retry():
    cause, advice, _ = describe_failure(UNCONFIRMED)
    assert "не подтвердил" in cause or "не подтверждён" in cause
    assert "заново" in advice


def test_unknown_hold_is_not_generic_stage_error():
    cause, _, _ = describe_failure("research: PHOTO_AUDIT_HOLD: что-то новое")
    assert not cause.startswith("ошибка этапа")


def _buttons(markup):
    return [b["callback_data"] for row in json.loads(markup)["inline_keyboard"] for b in row]


def test_markup_photo_hint_instead_of_retry_for_hopeless_holds():
    assert _buttons(failure_markup("c1", BRAND)) == ["exphoto:c1"]
    assert _buttons(failure_markup("c1", SOURCE)) == ["exphoto:c1"]
    assert _buttons(failure_markup("c1", UNCONFIRMED)) == ["exretry:c1"]
    assert _buttons(failure_markup("c1", "research: timeout")) == ["exretry:c1"]


def _reply_to_alert(code, action="exphoto", caption=""):
    return {"caption": caption, "reply_to_message": {"reply_markup": {"inline_keyboard": [
        [{"text": "📸", "callback_data": f"{action}:{code}"}]]}}}


def test_reply_photo_to_failure_alert_is_recognised():
    assert preview_code_from_reply(_reply_to_alert("abc", "exphoto")) == "abc"
    assert preview_code_from_reply(_reply_to_alert("abc", "exretry")) == "abc"


def test_reply_photo_to_failed_item_restarts_clean_with_caption_as_utp(tmp_path):
    db = tmp_path / "state.db"
    store = ExcelStore(db)
    name = "Обогреватель инфракрасный 1,5 кВт"
    store.select_items([("excel|aru:71300", "", name, name, 4000)])
    store.update("excel|aru:71300", status="failed", error=BRAND,
                 research_request_key="old-research", research_job=5634)
    links = OrderLinks(db)
    code = links.code_for("excel|aru:71300")
    fn = make_manual_photo_fn(db, links, ConfirmStore(db),
                              make_regen_fn(tmp_path / "cards.db", db), tmp_path / "manual")
    reply = fn(_reply_to_alert(code, caption="Мощность 1,5 кВт\nКварцевые лампы"), b"photo")
    assert "Фото принято" in reply
    item = store.get("excel|aru:71300")
    assert item.status == "new" and item.research_job is None and item.error is None
    with store._c() as c:
        assert c.execute("SELECT research_request_key, card_request_key FROM excel_items "
                         "WHERE key='excel|aru:71300'").fetchone() == (None, None)
    utp, photo = store.cache_get(f"|{name.lower()}")
    assert utp == "✓ Мощность 1,5 кВт\n✓ Кварцевые лампы"
    assert photo.endswith(".jpg")
