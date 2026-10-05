import json

import pytest

from content_factory.bot.control_guard import protect, validate_controls


VALID = b'''from menu import ControlMenu
def avito_markup(status): pass
def generation_markup(status): pass
def make_sources_fn(path): pass
def sources_markup(path): pass
def toggle_tg_source(path, data): pass
def main():
    def avito_fn(arg): pass
    def generation_fn(arg): pass
    def generation_state_fn(): pass
    control = ControlMenu()
    data = "avito:"
    data_g = "generation:"
    data_s = "srctg:"
    handle_command("/avito", avito_fn=avito_fn, generation_fn=generation_fn, generation_state_fn=generation_state_fn, sources_fn=make_sources_fn(None))
'''


def test_overwritten_bot_is_restored_without_touching_database(tmp_path):
    entry = tmp_path / "run.py"
    state = tmp_path / "controls"
    entry.write_bytes(VALID)
    assert protect(entry, state) == "controls_verified"
    entry.write_bytes(b'def main(): handle_command("/avito")')
    assert protect(entry, state) == "restored_last_good_controls"
    assert entry.read_bytes() == VALID
    assert len(list(state.glob("rejected-*.py"))) == 1


def test_corrupt_backup_cannot_be_run(tmp_path):
    entry = tmp_path / "run.py"
    state = tmp_path / "controls"
    entry.write_bytes(VALID)
    protect(entry, state)
    (state / "last-good.py").write_bytes(VALID + b"\n# corrupted")
    entry.write_bytes(b"def main(): pass")
    with pytest.raises(ValueError, match="checksum"):
        protect(entry, state)
    assert entry.read_bytes() == b"def main(): pass"


def test_advertised_command_without_routing_is_rejected():
    with pytest.raises(ValueError):
        validate_controls(VALID.replace(b", avito_fn=avito_fn", b""))


def test_new_valid_deployment_becomes_recovery_snapshot(tmp_path):
    entry = tmp_path / "run.py"
    state = tmp_path / "controls"
    entry.write_bytes(VALID)
    protect(entry, state)
    updated = VALID + b"\n# preserved VK and Telegram changes\n"
    entry.write_bytes(updated)
    protect(entry, state)
    assert (state / "last-good.py").read_bytes() == updated


def test_avito_without_master_generation_routing_is_not_a_valid_backup():
    with pytest.raises(ValueError, match="generation controls"):
        validate_controls(VALID.replace(b", generation_fn=generation_fn", b""))


def test_deployment_without_source_callbacks_is_rejected():
    with pytest.raises(ValueError):
        validate_controls(VALID.replace(b'data_s = "srctg:"', b'data_s = "lost"'))
