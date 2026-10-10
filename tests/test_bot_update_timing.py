from content_factory.bot import run as bot


def test_update_label_has_no_message_text():
    # в журнал времени попадают только тип обновления и имя команды/префикс кнопки
    assert bot._upd_label({"callback_query": {"data": "wizard:confirm:secret"}}) == ("cb", "wizard")
    assert bot._upd_label({"message": {"text": "/excel retry всё"}}) == ("cmd", "/excel")
    assert bot._upd_label({"message": {"text": "цена 25990, звоните"}}) == ("text", "-")
    assert bot._upd_label({"message": {"photo": [{}]}}) == ("photo", "-")
    assert bot._upd_label({}) == ("other", "-")


def test_update_timing_logs_previous_update_duration(capsys):
    prev = bot._upd_timing_start(None, {"message": {"text": "/status", "date": 0}})
    assert prev[:2] == ("cmd", "/status")
    assert bot._upd_timing_done(prev) is None
    out = capsys.readouterr().out
    assert out.startswith("upd cmd /status work=") and "pickup_age=" in out
    assert "status" in out and "секрет" not in out


def test_update_timing_without_previous_is_silent(capsys):
    assert bot._upd_timing_done(None) is None
    assert capsys.readouterr().out == ""
