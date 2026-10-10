"""Runtime supplier labels may change without changing slot identities."""
import json

import pytest

from content_factory.ingest.source_names import get_source_names, source_name


def write_names(path, value):
    (path / "source_names.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


def test_missing_config_preserves_confirmed_supplier_slot_identities(tmp_path):
    assert source_name(tmp_path, "manual__быттехопт_auto") == "manual__быттехопт_auto"
    assert source_name(tmp_path, "splithub") == "splithub"
    assert source_name(tmp_path, "aru") == "aru"
    assert get_source_names(tmp_path) == {}


@pytest.mark.parametrize("slot", ["mail__unknown", "manual__aru", "ARU", "manual"])
def test_unknown_slots_keep_exact_identity_not_guessed_supplier(tmp_path, slot):
    assert source_name(tmp_path, slot) == slot


def test_runtime_file_names_known_and_new_slots(tmp_path):
    write_names(tmp_path, {"aru": "АРУ инструменты", "mail__new": "Новый поставщик",
                          "splithub": "Мой СплитХаб"})
    assert source_name(tmp_path, "aru") == "АРУ инструменты"
    assert source_name(tmp_path, "mail__new") == "Новый поставщик"
    assert get_source_names(tmp_path)["splithub"] == "Мой СплитХаб"


def test_runtime_edits_are_seen_immediately_without_restart(tmp_path):
    write_names(tmp_path, {"aru": "Первое"})
    assert source_name(tmp_path, "aru") == "Первое"
    write_names(tmp_path, {"aru": "Второе"})  # Same byte length; no mtime cache assumption.
    assert source_name(tmp_path, "aru") == "Второе"
    (tmp_path / "source_names.json").unlink()
    assert source_name(tmp_path, "aru") == "aru"


def test_windows_utf8_bom_and_trimmed_labels_are_supported(tmp_path):
    (tmp_path / "source_names.json").write_text(
        json.dumps({"aru": "  Мой АРУ  "}, ensure_ascii=False), encoding="utf-8-sig"
    )
    assert source_name(tmp_path, "aru") == "Мой АРУ"


@pytest.mark.parametrize("raw", ["{", "[]", '"wrong shape"', "null", "true", "42"])
def test_broken_or_non_object_files_do_not_break_supplier_menu(tmp_path, raw):
    (tmp_path / "source_names.json").write_text(raw, encoding="utf-8")
    assert source_name(tmp_path, "aru") == "aru"
    assert source_name(tmp_path, "mail__unknown") == "mail__unknown"


def test_invalid_labels_are_ignored_without_hiding_valid_entries(tmp_path):
    write_names(tmp_path, {
        "aru": " ", "splithub": None, "manual__быттехопт_auto": 1,
        "mail__good": "Поставщик", "mail__bad": ["Wrong"], "": "Empty slot",
        " aru": "Changed slot identity",
    })
    assert source_name(tmp_path, "aru") == "aru"
    assert source_name(tmp_path, "splithub") == "splithub"
    assert source_name(tmp_path, "manual__быттехопт_auto") == "manual__быттехопт_auto"
    assert source_name(tmp_path, "mail__good") == "Поставщик"
    assert source_name(tmp_path, "mail__bad") == "mail__bad"
    assert "" not in get_source_names(tmp_path)
    assert " aru" not in get_source_names(tmp_path)


def test_each_call_returns_an_independent_mapping(tmp_path):
    first = get_source_names(tmp_path)
    first["aru"] = "Accidental caller mutation"
    assert source_name(tmp_path, "aru") == "aru"


def test_read_only_loader_does_not_create_runtime_files(tmp_path):
    get_source_names(tmp_path)
    assert not list(tmp_path.iterdir())


def test_unreadable_runtime_file_falls_back_to_exact_slot(tmp_path):
    (tmp_path / "source_names.json").mkdir()
    assert source_name(tmp_path, "aru") == "aru"
