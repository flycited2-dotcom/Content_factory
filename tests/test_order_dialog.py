"""Состояние диалога заказа клиента (задача 3): кол-во → комментарий → лид.
Чистый стор в SQLite (как WizardStore), переживает рестарт cf-bot."""
import sqlite3
from content_factory.bot.order_dialog import OrderDialogStore


def test_migrates_legacy_table_without_comment(tmp_path):
    # прод-грабля 2026-07-05: таблица создана в волне 1 БЕЗ колонки comment;
    # OrderDialogStore.__init__ должен доальтерить её, иначе snapshot падает
    # (no such column: comment) и роняет long-poll cf-bot в крэш-луп.
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE order_dialog (chat_id TEXT PRIMARY KEY, step TEXT, "
                "key TEXT, qty INTEGER)")
    con.execute("INSERT INTO order_dialog VALUES('1','awaiting_comment','k',2)")
    con.commit()
    con.close()
    s = OrderDialogStore(db)                    # init обязан ALTER добавить comment
    st = s.snapshot("1")                        # не должно падать
    assert st.step == "awaiting_comment" and st.qty == 2 and st.comment is None
    s.set_comment("1", "тест")
    assert s.snapshot("1").comment == "тест"


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


def test_set_comment_moves_to_phone(tmp_path):
    s = OrderDialogStore(tmp_path / "s.db")
    s.start("777", "k")
    s.set_qty("777", 2)
    s.set_comment("777", "нужна доставка")
    st = s.snapshot("777")
    assert st.comment == "нужна доставка" and st.step == "awaiting_phone"


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
