"""Состояние диалога заказа клиента (задача 3): кол-во → комментарий → лид.
Чистый стор в SQLite (как WizardStore), переживает рестарт cf-bot."""
from content_factory.bot.order_dialog import OrderDialogStore


def test_start_sets_awaiting_qty(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "breeze|funai|x")
    st = s.snapshot("777")
    assert st.step == "awaiting_qty" and st.key == "breeze|funai|x" and st.qty is None


def test_set_qty_moves_to_comment(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "k")
    s.set_qty("777", 3)
    st = s.snapshot("777")
    assert st.qty == 3 and st.step == "awaiting_comment"


def test_set_step_custom(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "k")
    s.set_step("777", "awaiting_qty_custom")
    assert s.snapshot("777").step == "awaiting_qty_custom"


def test_cancel_clears(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "k")
    s.cancel("777")
    assert s.snapshot("777") is None


def test_start_overwrites(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "k1")
    s.set_qty("777", 2)
    s.start("777", "k2")                       # новая заявка — с нуля
    st = s.snapshot("777")
    assert st.key == "k2" and st.qty is None and st.step == "awaiting_qty"


def test_snapshot_missing_is_none(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    assert s.snapshot("nope") is None
