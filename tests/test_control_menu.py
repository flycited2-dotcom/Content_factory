from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from content_factory.bot.control_menu import ControlMenu, COMMANDS, HOME, SOURCES, keyboard


def menu(tmp_path, run=None):
    return ControlMenu(tmp_path / "menu.db", "123", run=run)


@pytest.mark.parametrize("chat,sender", [("456", "456"), ("123", "456"), ("-100", "123")])
def test_private_owner_only(tmp_path, chat, sender):
    run = Mock()
    m = menu(tmp_path, run)
    assert m.handle("🩺 Состояние сервисов", chat, sender) is None
    run.assert_not_called()


def test_no_owner_fails_closed(tmp_path):
    assert ControlMenu(tmp_path / "x.db", "").handle("/menu", "123", "123") is None


def test_navigation_persists_and_preserves_order_links(tmp_path):
    m = menu(tmp_path)
    reply = m.handle("/menu@Sendpr1ce_bot", "123", "123")
    assert "Главное меню" in reply.text
    assert len(reply.markup["keyboard"]) == 3
    m.handle("📦 Прайсы поставщиков", "123", "123")
    assert menu(tmp_path).state("123")[0] == "prices"
    assert m.handle("/start ord_123", "123", "123") is None
    assert m.handle("/approve abc", "123", "123") is None
    assert m.handle("просто текст", "123", "123") is None


@pytest.mark.parametrize("label,command", list(COMMANDS.items()))
def test_existing_action_routes(tmp_path, label, command):
    assert menu(tmp_path).handle(label, "123", "123").command == command


def test_cross_bot_link_and_upload(tmp_path):
    m = menu(tmp_path)
    assert "S1mfer_bot" in m.handle("/collect", "123", "123").text
    assert ".xlsx" in m.handle("📥 Загрузить прайс", "123", "123").text
    assert [HOME] in keyboard("prices")["keyboard"]


def test_start_requires_source_and_single_use_confirmation(tmp_path):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    m = menu(tmp_path, run)
    assert "Сначала" in m.handle("🔄 Обновить источник", "123", "123").text
    source = next(iter(SOURCES))
    m.handle(source, "123", "123")
    m.handle("▶️ Подтвердить обновление", "123", "123")
    run.assert_not_called()
    m.handle("🔄 Обновить источник", "123", "123")
    run.assert_not_called()
    result = m.handle("▶️ Подтвердить обновление", "123", "123")
    assert "не подтверждено" in result.text
    assert run.call_args.args[0] == ["systemctl", "start", "--no-block", SOURCES[source]]
    m.handle("▶️ Подтвердить обновление", "123", "123")
    assert run.call_count == 1


def test_navigation_invalidates_confirmation(tmp_path):
    run = Mock()
    m = menu(tmp_path, run)
    m.handle(next(iter(SOURCES)), "123", "123")
    m.handle("🔄 Обновить источник", "123", "123")
    m.handle(HOME, "123", "123")
    m.handle("▶️ Подтвердить обновление", "123", "123")
    run.assert_not_called()


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 10)])
def test_service_failure_is_visible(tmp_path, failure):
    m = menu(tmp_path, Mock(side_effect=failure))
    assert "недоступна" in m.handle("🩺 Состояние сервисов", "123", "123").text


def test_no_arbitrary_command_or_shell(tmp_path):
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
    m = menu(tmp_path, run)
    assert m.handle("systemctl start injected.service", "123", "123") is None
    assert "не выполнена" in m.handle("🗓 Расписание сервисов", "123", "123").text
    assert "shell" not in run.call_args.kwargs
