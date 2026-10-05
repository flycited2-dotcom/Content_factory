from datetime import datetime

from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.orchestrator.excel_pipeline import ExcelStore


def flow(tmp_path, wake, due_at=None):
    state = tmp_path / "state.db"
    wizard = WizardStore(state)
    wizard.start("owner")
    wizard.set_pick("owner", [("excel|beko|a1", "Beko", "A1", "Стиральная машина Beko A1", 100)])
    wizard.set_time("owner", due_at)
    wizard.set_photo("owner", None)
    wizard.set_utp("owner", None)
    *_, confirm = make_wizard_flow(
        state, tmp_path / "prices", wizard, lambda *a, **kw: 999,
        lambda *a: "", lambda: "status", wake_fn=wake,
        now_fn=lambda: datetime(2026, 10, 2, 11, 0))
    return state, wizard, confirm


def test_wake_after_committed_selection_once(tmp_path):
    calls = []
    def wake():
        assert ExcelStore(tmp_path / "state.db").latest_selection()["keys"] == ["excel|beko|a1"]
        calls.append("wake")
        return True
    state, wizard, confirm = flow(tmp_path, wake)
    reply = confirm("owner", "wizard:confirm")
    assert calls == ["wake"]
    assert "Запуск конвейера запрошен" in reply.text
    assert ExcelStore(state).get("excel|beko|a1").status == "new"
    confirm("owner", "wizard:confirm")
    assert calls == ["wake"]


def test_future_selection_is_saved_without_immediate_wake(tmp_path):
    calls = []
    state, wizard, confirm = flow(tmp_path, lambda: calls.append(1),
                                  due_at=datetime(2026, 10, 3, 11, 0).timestamp())
    confirm("owner", "wizard:confirm")
    assert not calls
    assert len(ExcelStore(state).scheduled(now=datetime(2026, 10, 2, 11, 0).timestamp())) == 1


def test_failed_wake_does_not_lose_accepted_task(tmp_path):
    def wake():
        raise OSError("offline")
    state, wizard, confirm = flow(tmp_path, wake)
    reply = confirm("owner", "wizard:confirm")
    assert "Задачи сохранены" in reply.text
    assert ExcelStore(state).get("excel|beko|a1").status == "new"
