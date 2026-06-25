from content_factory.orchestrator.confirm_store import ConfirmStore


def test_add_and_get(tmp_path):
    s = ConfirmStore(tmp_path / "s.db")
    s.add("k1", "@chan", "/c/k1.jpg", "подпись")
    a = s.get("k1")
    assert a.key == "k1" and a.channel == "@chan"
    assert a.card_path == "/c/k1.jpg" and a.caption == "подпись"
    assert a.status == "pending"


def test_get_missing_returns_none(tmp_path):
    assert ConfirmStore(tmp_path / "s.db").get("nope") is None


def test_list_pending(tmp_path):
    s = ConfirmStore(tmp_path / "s.db")
    s.add("k1", "@c", "p1", "c1")
    s.add("k2", "@c", "p2", "c2")
    s.mark("k1", "published")
    assert [a.key for a in s.list_pending()] == ["k2"]


def test_mark_changes_status(tmp_path):
    s = ConfirmStore(tmp_path / "s.db")
    s.add("k1", "@c", "p", "c")
    s.mark("k1", "rejected")
    assert s.get("k1").status == "rejected"


def test_add_is_upsert_resets_pending(tmp_path):
    s = ConfirmStore(tmp_path / "s.db")
    s.add("k1", "@c", "p", "c")
    s.mark("k1", "rejected")
    s.add("k1", "@c2", "p2", "c2")          # повторная отправка на подтверждение
    a = s.get("k1")
    assert a.channel == "@c2" and a.caption == "c2" and a.status == "pending"
