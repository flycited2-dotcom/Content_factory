from datetime import date
import pytest
from content_factory.bot.commands import parse_plan, handle_command, handle_callback
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.telegram import PublishState, PublishResult

TODAY = date(2026, 6, 25)


def test_parse_basic_command():
    t = parse_plan("/plan 10 кондиционеры завтра 10:00,14:00 mode=mcp", today=TODAY)
    assert t.count == 10
    assert t.filter == {"categories": [2, 6, 7]}
    assert t.mode == "mcp"
    assert t.schedule == ["2026-06-26 10:00", "2026-06-26 14:00"]
    assert t.confirm is False


def test_parse_segodnya_and_padding():
    t = parse_plan("/plan 5 кондиционеры сегодня 9:00", today=TODAY)
    assert t.schedule == ["2026-06-25 09:00"]


def test_parse_explicit_date():
    t = parse_plan("/plan 5 кондиционеры 2026-07-01 11:00", today=TODAY)
    assert t.schedule == ["2026-07-01 11:00"]


def test_parse_source_and_cat_override():
    t = parse_plan("/plan 8 завтра 10:00 source=breeze cat=2,6", today=TODAY)
    assert t.filter == {"categories": [2, 6], "source": "breeze"}


def test_parse_confirm_and_channel_and_id():
    t = parse_plan("/plan 3 кондиционеры завтра 10:00 confirm channel=@x id=myid", today=TODAY)
    assert t.confirm is True and t.channel == "@x" and t.id == "myid"


def test_parse_missing_count_raises():
    with pytest.raises(ValueError, match="количество"):
        parse_plan("/plan кондиционеры завтра 10:00", today=TODAY)


def test_parse_missing_time_raises():
    with pytest.raises(ValueError, match="врем"):
        parse_plan("/plan 5 кондиционеры завтра", today=TODAY)


def test_parse_missing_filter_raises():
    with pytest.raises(ValueError, match="категори"):
        parse_plan("/plan 5 завтра 10:00", today=TODAY)


def test_handle_plan_adds_to_queue(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/plan 10 кондиционеры завтра 10:00,14:00 mode=mcp", q, today=TODAY)
    assert "✅" in reply or "ок" in reply.lower()
    assert len(q.all_slots()) == 2


def test_handle_status(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    handle_command("/plan 5 кондиционеры завтра 10:00 id=t1", q, today=TODAY)
    reply = handle_command("/status", q, today=TODAY)
    assert "t1" in reply or "1" in reply        # есть инфо о задаче/слотах


def test_status_collapses_done_lists_pending(tmp_path):
    # жалоба владельца 2026-07-07: /status — бесконечная простыня done-слотов
    # прошлых дней; выполненное — одной сводной строкой, ожидающее — детально
    q = TaskQueue(tmp_path / "q.db")
    handle_command("/plan 5 кондиционеры завтра 10:00 id=old-1", q, today=TODAY)
    handle_command("/plan 6 кондиционеры завтра 14:00 id=old-2", q, today=TODAY)
    for s in q.all_slots():
        q.mark_done(s.task_id, s.due_at)        # старые отработали
    handle_command("/plan 5 кондиционеры завтра 10:00 id=fresh", q, today=TODAY)

    reply = handle_command("/status", q, today=TODAY)

    assert "old-1" not in reply and "old-2" not in reply   # done не перечисляются
    assert "fresh" in reply                                # ожидающие — поимённо
    assert "2" in reply                                    # сводка выполненных задач


def test_handle_cancel(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    handle_command("/plan 5 кондиционеры завтра 10:00 id=t1", q, today=TODAY)
    reply = handle_command("/cancel t1", q, today=TODAY)
    assert "t1" in reply
    assert all(s.status == "cancelled" for s in q.all_slots())


def test_handle_unknown_returns_help(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/foobar", q, today=TODAY)
    assert "/plan" in reply        # подсказка по командам


def test_handle_invalid_plan_returns_error(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/plan завтра 10:00", q, today=TODAY)   # нет count
    assert "❌" in reply or "ошибк" in reply.lower()
    assert q.all_slots() == []


def test_handle_approve_publishes(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    ps = PublishState(tmp_path / "p.db")
    cs.add("breeze|ballu|olympio", "@chan", "/c/x.jpg", "подпись")
    sent = {}

    def publish_fn(a):
        sent["key"] = a.key
        return PublishResult(ok=True, message_id=11)

    reply = handle_command("/approve breeze|ballu|olympio", q,
                           confirm_store=cs, publish_fn=publish_fn, publish_state=ps)
    assert "✅" in reply
    assert sent["key"] == "breeze|ballu|olympio"
    assert cs.get("breeze|ballu|olympio").status == "published"
    assert ps.is_published("breeze|ballu|olympio")
    # подпись поста должна сохраниться (нужна для лида «Заказать»), а не затереться
    rec = next(r for r in ps.records() if r.key == "breeze|ballu|olympio")
    assert rec.caption == "подпись" and rec.channel == "@chan"


def test_handle_approve_key_with_spaces(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    ps = PublishState(tmp_path / "p.db")
    key = "breeze|expertair by zilon|progress"        # ключ серии с пробелами
    cs.add(key, "@chan", "/c/x.jpg", "cap")
    got = {}

    def publish_fn(a):
        got["key"] = a.key
        return PublishResult(ok=True, message_id=5)

    reply = handle_command(f"/approve {key}", q, confirm_store=cs,
                           publish_fn=publish_fn, publish_state=ps)
    assert "✅" in reply and got["key"] == key
    assert cs.get(key).status == "published"


def test_handle_reject_key_with_spaces(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    key = "breeze|expertair by zilon|progress"
    cs.add(key, "@chan", "/c/x.jpg", "cap")
    handle_command(f"/reject {key}", q, confirm_store=cs)
    assert cs.get(key).status == "rejected"


def test_handle_approve_unknown_key(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    reply = handle_command("/approve nope", q, confirm_store=cs, publish_fn=lambda a: None)
    assert "❌" in reply


def test_handle_approve_publish_failure_keeps_pending(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    cs.add("k1", "@chan", "/c/x.jpg", "cap")

    def publish_fn(a):
        return PublishResult(ok=False, error="chat not found")

    reply = handle_command("/approve k1", q, confirm_store=cs, publish_fn=publish_fn)
    assert "❌" in reply and "chat not found" in reply
    assert cs.get("k1").status == "pending"          # не публикуем — остаётся ждать


def test_handle_reject(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    cs.add("k1", "@chan", "/c/x.jpg", "cap")
    reply = handle_command("/reject k1", q, confirm_store=cs)
    assert "k1" in reply
    assert cs.get("k1").status == "rejected"


def test_handle_pending_lists(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    cs.add("k1", "@chan", "/c/x.jpg", "cap")
    reply = handle_command("/pending", q, confirm_store=cs)
    assert "k1" in reply


def test_handle_callback_approve_publishes(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    ps = PublishState(tmp_path / "p.db")
    key = "breeze|funai|daijin inverter"          # ключ с пробелом — кнопка несёт его целиком
    cs.add(key, "@chan", "/c/x.jpg", "cap")
    got = {}

    def publish_fn(a):
        got["key"] = a.key
        return PublishResult(ok=True, message_id=1)

    reply = handle_callback(f"approve:{key}", q, confirm_store=cs,
                            publish_fn=publish_fn, publish_state=ps)
    assert "✅" in reply and got["key"] == key
    assert cs.get(key).status == "published"


def test_handle_callback_reject(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    cs.add("k1", "@chan", "/c/x.jpg", "cap")
    reply = handle_callback("reject:k1", q, confirm_store=cs)
    assert "k1" in reply and cs.get("k1").status == "rejected"


def test_handle_callback_bad_data(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    assert "❌" in handle_callback("garbage", q)


# ── /regen и кнопка 🔄 (перегенерация карточки по запросу владельца) ──────────
def test_handle_regen_marks_and_calls_fn(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    key = "breeze|xigma|sky inverter"
    cs.add(key, "@chan", "/c/x.jpg", "cap")
    got = {}

    def regen_fn(a):
        got["key"] = a.key
        return True

    reply = handle_command(f"/regen {key}", q, confirm_store=cs, regen_fn=regen_fn)
    assert "🔄" in reply and got["key"] == key
    assert cs.get(key).status == "regen"


def test_handle_regen_unknown_key(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    reply = handle_command("/regen nope", q, confirm_store=cs, regen_fn=lambda a: True)
    assert "❌" in reply


def test_handle_callback_regen(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cs = ConfirmStore(tmp_path / "c.db")
    cs.add("k1", "@chan", "/c/x.jpg", "cap")
    reply = handle_callback("regen:k1", q, confirm_store=cs, regen_fn=lambda a: True)
    assert "🔄" in reply and cs.get("k1").status == "regen"


# ── /make: выбор товаров из Excel-прайса (подпроект 3) ────────────────────────
def test_parse_make_full():
    from content_factory.bot.commands import parse_make
    count, cat, quotas = parse_make("/make 10 холодильники beko=3 indesit=3 stinol=*")
    assert (count, cat) == (10, "холодильники")
    assert quotas == {"beko": 3, "indesit": 3, "stinol": None, "*": None}


def test_parse_make_rest_word():
    from content_factory.bot.commands import parse_make
    _, _, quotas = parse_make("/make 5 стиральные beko=2 candy=остальные")
    assert quotas == {"beko": 2, "candy": None, "*": None}


def test_parse_make_errors():
    from content_factory.bot.commands import parse_make
    import pytest
    with pytest.raises(ValueError):
        parse_make("/make холодильники")           # нет количества
    with pytest.raises(ValueError):
        parse_make("/make 5")                      # нет категории


def test_parse_make_count_must_be_first_token():
    # грабля 2026-07-03: владелец прислал список моделей («…Stinol WSTU 410 C…»)
    # без /make N — число «410» из середины подхватилось как count (запрошено 410,
    # категория молча стала «стиральная»). Теперь — явная ошибка вместо угадывания.
    from content_factory.bot.commands import parse_make
    import pytest
    with pytest.raises(ValueError):
        parse_make("/make Стиральная машина Stinol WSTU 410 C")


def test_handle_make_calls_fn(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    got = {}

    def make_fn(count, category, quotas):
        got.update(count=count, category=category, quotas=quotas)
        return "✅ выбрано 3"
    reply = handle_command("/make 3 холодильники beko=1", q, make_fn=make_fn)
    assert reply == "✅ выбрано 3" and got["count"] == 3
    assert handle_command("/make 3 холодильники", q) == "❌ excel-источник недоступен"


# ── /find, /pick, /excel: выбор конкретных моделей и статус конвейера ─────────
def test_handle_find_and_pick(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    got = {}
    reply = handle_command("/find генераторы carver", q,
                           find_fn=lambda ph: got.update(ph=ph) or "1. CARVER …")
    assert reply == "1. CARVER …" and got["ph"] == "генераторы carver"
    reply = handle_command("/pick 1, 3 5", q,
                           pick_fn=lambda nums: got.update(nums=nums) or "✅ взято 3")
    assert reply == "✅ взято 3" and got["nums"] == [1, 3, 5]
    assert "❌" in handle_command("/pick abc", q, pick_fn=lambda n: "x")
    assert "❌" in handle_command("/find", q, find_fn=lambda ph: "x")


def test_handle_excel_status(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/excel", q, excel_fn=lambda: "research 2 | card 1")
    assert "research 2" in reply
