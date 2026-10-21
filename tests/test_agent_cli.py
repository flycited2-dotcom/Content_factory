"""agent_cli — JSON-интерфейс постановки задач для оркестратора (Agents_MY, этап 2).

Оркестратор не пишет в базу сам: он вызывает эти подкоманды, а они используют
тот же код, что /make и кнопка «Отменить» в боте.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from content_factory import agent_cli
from content_factory.bot.commands import parse_due_at
from content_factory.orchestrator.excel_pipeline import ExcelStore


@pytest.fixture
def env(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Холодильники", "", "", "", "", ""])
    ws.append(["1", "10", "Beko", "Холодильник Beko X100", "30000", ""])
    ws.append(["2", "11", "Candy", "Холодильник Candy Y200", "25000", ""])
    prices = tmp_path / "prices"
    prices.mkdir()
    wb.save(prices / "manual.xlsx")
    state = tmp_path / "state.db"
    base = ["--state-db", str(state), "--prices-dir", str(prices), "--queue-db", "", "--no-wake"]
    return state, base


def _run(capsys, args):
    code = agent_cli.main(args)
    return code, json.loads(capsys.readouterr().out)


def test_hint_lists_brands_from_current_prices(env, capsys):
    _, base = env
    code, out = _run(capsys, ["hint", *base])
    assert code == 0
    assert set(out["brands"]) == {"Beko", "Candy"}
    assert out["sources"] == ["manual"]


def test_resolve_has_no_side_effects(env, capsys):
    state, base = env
    code, out = _run(capsys, ["resolve", "--category", "холодильники", *base])
    assert code == 0
    assert out["total_matches"] == 2
    assert {i["key"] for i in out["items"]} == {"excel|beko|x100", "excel|candy|y200"}
    assert ExcelStore(state).by_status("new") == []


def test_resolve_count_limits_items_but_not_total(env, capsys):
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--count", "1", *base])
    assert len(out["items"]) == 1
    assert out["total_matches"] == 2


def test_enqueue_selects_with_due_time(env, capsys):
    state, base = env
    now = datetime.now()
    code, out = _run(capsys, ["enqueue", "--category", "холодильники", "--due", "завтра 12:00", *base])
    assert code == 0
    assert set(out["accepted"]) == {"excel|beko|x100", "excel|candy|y200"}
    assert out["due_at"] == parse_due_at("завтра 12:00", now)
    # до срока задачи скрыты от тика (расписание /task), после — видны
    assert ExcelStore(state).by_status("new") == []
    assert {i.key for i in ExcelStore(state).by_status("new", now=out["due_at"] + 1)} == set(out["accepted"])


def test_enqueue_twice_accepts_nothing_second_time(env, capsys):
    _, base = env
    _run(capsys, ["enqueue", "--category", "холодильники", *base])
    _, out = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    assert out["accepted"] == []


def test_cancel_marks_items_cancelled(env, capsys):
    state, base = env
    _, enq = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    code, out = _run(capsys, ["cancel", "--keys", ",".join(enq["accepted"]), *base])
    assert code == 0
    assert out["cancelled"] == 2
    assert ExcelStore(state).by_status("new") == []


def test_unknown_command_or_bad_quotas_exit_2(env, capsys):
    _, base = env
    with pytest.raises(SystemExit) as exc:
        agent_cli.main(["nope", *base])
    assert exc.value.code == 2
    capsys.readouterr()
    code, out = _run(capsys, ["resolve", "--category", "x", "--quotas", "{bad", *base])
    assert code == 2
    assert "error" in out


def test_brand_all_quota_limits_to_that_brand(env, capsys):
    # «все холодильники beko» → {"beko": null} без count: только Beko, а не вся категория
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--quotas", '{"beko": null}', *base])
    assert [i["key"] for i in out["items"]] == ["excel|beko|x100"]
    assert out["total_matches"] == 1


def test_mixed_quotas_keep_numbered_brand_and_all_of_null_brand(env, capsys):
    # «1 Candy и все Beko» → {"candy": 1, "beko": null}: обе марки, без добора чужих
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--quotas", '{"candy": 1, "beko": null}', *base])
    assert sorted(i["key"] for i in out["items"]) == ["excel|beko|x100", "excel|candy|y200"]


def _queue(tmp_path, lanes, commands=()):
    import sqlite3
    q = tmp_path / "queue.db"
    with sqlite3.connect(q) as db:
        db.execute("CREATE TABLE lane_heartbeat (lane TEXT PRIMARY KEY, seen_at TEXT)")
        db.execute("CREATE TABLE flags (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO lane_heartbeat VALUES (?,?)", lanes)
        db.executemany("INSERT INTO flags VALUES (?,?)", commands)
    return q


def test_workers_reports_generation_lanes_and_commands(env, capsys, tmp_path):
    from datetime import timedelta
    state, base = env
    now = datetime.now()
    q = _queue(tmp_path, [("desktop-a1", (now - timedelta(seconds=30)).isoformat()),
                          ("laptop-a1", (now - timedelta(days=7)).isoformat())], [("agent_command_laptop", "stop")])
    args = [a if a != "" else str(q) for a in base]                       # --queue-db <queue>
    code, out = _run(capsys, ["workers", *args])
    assert code == 0
    assert out["generation"] is False                                        # без разрешения — выключена
    assert out["agent_online"] is True
    lanes = {l["lane"]: l for l in out["lanes"]}
    assert lanes["desktop-a1"]["online"] is True and lanes["laptop-a1"]["online"] is False
    assert out["commands"] == {"agent_command_laptop": "stop"}


def test_workers_offline_when_no_fresh_heartbeat(env, capsys, tmp_path):
    from datetime import timedelta
    _, base = env
    q = _queue(tmp_path, [("desktop-a1", (datetime.now() - timedelta(hours=14)).isoformat())])
    _, out = _run(capsys, ["workers", *[a if a != "" else str(q) for a in base]])
    assert out["agent_online"] is False
    assert out["last_seen"].startswith((datetime.now() - timedelta(hours=14)).date().isoformat())


def test_start_workers_enables_generation_and_commands_start(env, capsys, tmp_path):
    import sqlite3
    from content_factory.orchestrator.generation import generation_enabled
    state, base = env
    q = _queue(tmp_path, [], [("agent_command", "stop"), ("agent_command_laptop", "stop")])
    code, out = _run(capsys, ["start-workers", *[a if a != "" else str(q) for a in base]])
    assert code == 0 and out["generation"] is True
    assert generation_enabled(state)
    with sqlite3.connect(q) as db:
        flags = dict(db.execute("SELECT key, value FROM flags"))
    assert flags == {"agent_command": "start", "agent_command_laptop": "start", "agent_command_desktop": "start"}


def test_enqueue_wakes_agents_without_overriding_stop(env, capsys, tmp_path, monkeypatch):
    import sqlite3
    state, base = env
    q = _queue(tmp_path, [], [("agent_command_laptop", "stop")])
    args = [a for a in base if a != "--no-wake"]
    args = [a if a != "" else str(q) for a in args]
    monkeypatch.setattr("content_factory.bot.worker_control.request_run", lambda: True)
    _run(capsys, ["enqueue", "--category", "холодильники", *args])
    with sqlite3.connect(q) as db:
        flags = dict(db.execute("SELECT key, value FROM flags"))
    assert flags["agent_command_laptop"] == "stop"                           # ручной стоп не перебит
    assert flags["agent_command"] == "wake" and flags["agent_command_desktop"] == "wake"


def test_cancel_reports_items_that_could_not_be_cancelled(env, capsys):
    state, base = env
    _, enq = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    ExcelStore(state).update(enq["accepted"][0], status="preview")       # уже на превью — отменять поздно
    _, out = _run(capsys, ["cancel", "--keys", ",".join(enq["accepted"]), *base])
    assert out["cancelled"] == 1
    assert out["not_cancelled"] == [{"key": enq["accepted"][0], "status": "preview"}]


def test_cancel_reports_preview_already_rejected(env, capsys):
    from content_factory.orchestrator.confirm_store import ConfirmStore
    state, base = env
    _, enq = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    key = enq["accepted"][0]
    ExcelStore(state).update(key, status="preview")
    ConfirmStore(state).add(key, "tg", "card.png", "caption")
    ConfirmStore(state).mark(key, "rejected")                            # владелец уже нажал ❌
    _, out = _run(capsys, ["cancel", "--keys", key, *base])
    assert out["not_cancelled"] == [{"key": key, "status": "rejected"}]


# ── этап 3: картинка, публикация в TG, постановка в ВК-план ──

def _jobs_db(tmp_path, rows):
    import sqlite3
    q = tmp_path / "queue.db"
    with sqlite3.connect(q) as db:
        db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, output_filename TEXT, "
                   "error_text TEXT, request_key TEXT)")
        db.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", rows)
    return q


def test_visual_submit_reuses_live_job_and_resubmits_failed(env, capsys, tmp_path, monkeypatch):
    _, base = env
    sent = []
    monkeypatch.setattr(agent_cli, "_fotogen_submit", lambda api, token, data: sent.append(data) or 77)
    monkeypatch.setenv("FOTOGEN_API_URL", "http://fotogen")
    q = _jobs_db(tmp_path, [(5, "processing", None, None, "orch-a"), (6, "failed", None, "x", "orch-b")])
    args = [a if a != "" else str(q) for a in base]
    _, out = _run(capsys, ["visual-submit", "--scene", "кадр", "--request-key", "orch-a", *args])
    assert out == {"job_id": 5, "reused": True} and sent == []
    _, out = _run(capsys, ["visual-submit", "--scene", "кадр", "--request-key", "orch-b", *args])
    assert out == {"job_id": 77, "reused": False}
    assert sent[0]["category"] == "editorial" and sent[0]["request_key"] == "orch-b"


def test_visual_status_reports_path_only_for_existing_file(env, capsys, tmp_path, monkeypatch):
    _, base = env
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "ok.png").write_bytes(b"png")
    monkeypatch.setenv("FOTOGEN_OUTPUT_DIR", str(out_dir))
    q = _jobs_db(tmp_path, [(1, "done", "ok.png", None, "k1"), (2, "done", "lost.png", None, "k2"),
                            (3, "pending", None, None, "k3")])
    args = [a if a != "" else str(q) for a in base]
    assert _run(capsys, ["visual-status", "--job", "1", *args])[1]["path"] == str(out_dir / "ok.png")
    assert _run(capsys, ["visual-status", "--job", "2", *args])[1]["status"] == "missing_file"
    assert _run(capsys, ["visual-status", "--job", "3", *args])[1] == {"status": "pending", "path": None, "error": None}


def test_publish_tg_uses_editorial_key_and_returns_link(env, capsys, tmp_path, monkeypatch):
    from content_factory.publish.telegram import PublishResult
    _, base = env
    calls = {}

    def fake_publish(token, channel, image, caption, **kw):
        calls.update(channel=channel, caption=caption, key=kw["key"])
        calls["button"] = json.loads(kw["reply_markup"])["inline_keyboard"][0][0]
        return PublishResult(ok=True, message_id=42)

    monkeypatch.setattr("content_factory.publish.telegram.publish_post", fake_publish)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-1001234567")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cap = tmp_path / "cap.txt"
    cap.write_text("Текст поста", encoding="utf-8")
    img = tmp_path / "i.png"
    img.write_bytes(b"png")
    code, out = _run(capsys, ["publish-tg", "--key", "heat-pumps-v2", "--caption-file", str(cap),
                              "--image", str(img), *base])
    assert code == 0 and out["ok"] and out["url"] == "https://t.me/c/1234567/42"
    button = calls.pop("button")
    assert calls == {"channel": "-1001234567", "caption": "Текст поста", "key": "editorial|heat-pumps-v2"}
    assert button["text"] == "🔎 Каталог на сайте" and button["url"].startswith("https://splithome.ru/catalog/?utm_source=telegram")


def test_publish_rejects_unsafe_key(env, capsys, tmp_path):
    _, base = env
    code, out = _run(capsys, ["publish-tg", "--key", "../x", "--caption-file", "c", "--image", "i", *base])
    assert code == 2 and "id" in out["error"]


def test_plan_vk_adds_approved_item_once(env, capsys, tmp_path):
    from content_factory.orchestrator.vk_content_plan import VkContentPlanStore
    _, base = env
    vk_root = tmp_path / "vk"
    cap = tmp_path / "cap.txt"
    cap.write_text("Тепловой насос: как он греет дом зимой", encoding="utf-8")
    img = tmp_path / "i.png"
    img.write_bytes(b"png")
    args = ["plan-vk", "--key", "heat-pumps", "--caption-file", str(cap), "--image", str(img),
            "--vk-root", str(vk_root), *base]
    code, out = _run(capsys, args)
    assert code == 0 and out["ok"]
    item = VkContentPlanStore(vk_root / "state" / "vk-plan.db").get(out["plan_id"])
    assert item.status == "approved" and item.source_key == "agent:heat-pumps"
    assert Path(item.card_path) == (vk_root / "assets/generated/editorial/heat-pumps.png").resolve()
    _, again = _run(capsys, args)
    assert again["ok"] is False and "дубль" in again["error"]


def test_compose_adds_site_section_and_phone(env, capsys, tmp_path):
    _, base = env
    cap = tmp_path / "cap.txt"
    cap.write_text("Тепловой насос зимой: как он греет дом", encoding="utf-8")
    _, out = _run(capsys, ["compose", "--key", "hp-v4", "--channel", "vk_editorial", "--caption-file", str(cap), *base])
    text = out["text"]
    assert text.startswith("Тепловой насос зимой")
    assert "https://splithome.ru/catalog/heat-pumps/?utm_source=vk" in text and "utm_content=hp-v4" in text
    assert "📞 Консультация и подбор: +7 978 579-29-95" in text and "сообщения группы" in text
    cap.write_text("Как выбрать сплит-систему в спальню: кондиционер на 9 BTU", encoding="utf-8")
    _, out = _run(capsys, ["compose", "--key", "ac", "--channel", "tg_editorial", "--caption-file", str(cap), *base])
    assert "splithome.ru" not in out["text"] and "+7 978 579-29-95" in out["text"]   # ссылка в TG — кнопкой
    assert "сообщения группы" not in out["text"]
