"""Ввод аргументов команд через ForceReply (задача 2): тап пустой команды
(/find, /make, /pick) → бот присылает приглашение с полем ввода, следующий
текст владельца = аргумент. Здесь — чистая логика (стор + разбор), без Telegram."""
from content_factory.bot.cmd_input import (
    ARG_COMMANDS, bare_arg_command, prompt_for, resolve_reply, PendingCmdStore)


def test_bare_command_detected():
    assert bare_arg_command("/find") == "/find"
    assert bare_arg_command("  /make  ") == "/make"
    assert bare_arg_command("/pick") == "/pick"


def test_bare_command_case_insensitive_and_botname():
    assert bare_arg_command("/FIND") == "/find"
    assert bare_arg_command("/find@Sendpr1ce_bot") == "/find"


def test_command_with_arg_is_not_bare():
    assert bare_arg_command("/find генераторы carver") is None
    assert bare_arg_command("/make 10 холодильники") is None
    assert bare_arg_command("/pick 1 3 5") is None


def test_non_arg_commands_ignored():
    # эти команды работают сразу, без приглашения
    assert bare_arg_command("/excel") is None
    assert bare_arg_command("/status") is None
    assert bare_arg_command("/task") is None
    assert bare_arg_command("просто текст") is None
    assert bare_arg_command("") is None


def test_prompt_for_returns_text_and_placeholder():
    for cmd in ("/find", "/make", "/pick"):
        text, placeholder = prompt_for(cmd)
        assert text and placeholder
        assert cmd in ARG_COMMANDS


def test_resolve_reply_builds_command():
    assert resolve_reply("/find", "генераторы carver") == "/find генераторы carver"
    assert resolve_reply("/pick", " 1 3 5 ") == "/pick 1 3 5"


def test_resolve_reply_ignores_when_reply_is_command():
    # владелец вместо ввода набрал другую команду — отложенную игнорируем
    assert resolve_reply("/find", "/excel") is None


def test_resolve_reply_none_without_pending():
    assert resolve_reply(None, "текст") is None
    assert resolve_reply("/find", "") is None


def test_pending_store_set_take_is_one_shot(tmp_path):
    s = PendingCmdStore(tmp_path / "s.db")
    assert s.take("100") is None                 # пусто
    s.set("100", "/find")
    assert s.take("100") == "/find"              # взяли
    assert s.take("100") is None                 # одноразово — очистилось


def test_pending_store_overwrite_and_clear(tmp_path):
    s = PendingCmdStore(tmp_path / "s.db")
    s.set("100", "/find")
    s.set("100", "/make")                        # перезапись
    s.clear("100")
    assert s.take("100") is None
