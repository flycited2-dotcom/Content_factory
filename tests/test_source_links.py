"""Proof-based retirement of old supplier slots without deleting price files."""
import hashlib
import json

import openpyxl
import pytest

from content_factory.ingest.source_links import active_source_paths, source_refresh_metadata


def _price(path, *, empty=False, name="Холодильник Beko TEST", price=10000):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["№", "Артикул", "Бренд", "Наименование", "Цена"])
    sheet.append(["Холодильники", None, None, None, None])
    if not empty:
        sheet.append([1, "UT-001", "Beko", name, price])
    workbook.save(path)
    return path


def _replacement(old, new_slot="mail__simfer"):
    return {"old_slot": old.stem, "old_sha256": hashlib.sha256(old.read_bytes()).hexdigest(),
            "new_slot": new_slot}


def _registry(directory, entries, **overrides):
    content = {"schema_version": 1, "replacements": entries, **overrides}
    (directory / "price_links.json").write_text(json.dumps(content), encoding="utf-8")


def test_matching_old_copy_retires_only_with_usable_successor_and_keeps_files(tmp_path):
    old = _price(tmp_path / "mail.xlsx", price=9000)
    new = _price(tmp_path / "mail__simfer.xlsx", price=11000)
    unrelated = _price(tmp_path / "manual__other.xlsx")
    _registry(tmp_path, [_replacement(old)])
    paths = [(old.stem, old), (unrelated.stem, unrelated), (new.stem, new)]

    assert active_source_paths(tmp_path, paths) == paths[1:]
    assert old.exists() and new.exists() and unrelated.exists()
    assert paths[0] == ("mail", old)


def test_changed_old_copy_is_not_retired_by_previous_hash(tmp_path):
    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    _registry(tmp_path, [_replacement(old)])
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths[1:]

    _price(old, name="Другой товар", price=25000)
    assert active_source_paths(tmp_path, paths) == paths


@pytest.mark.parametrize("replacement_kind", ["missing", "broken", "empty"])
def test_missing_broken_or_empty_successor_keeps_old_price(tmp_path, replacement_kind):
    old = _price(tmp_path / "mail.xlsx")
    new = tmp_path / "mail__simfer.xlsx"
    if replacement_kind == "broken":
        new.write_bytes(b"This is not an Excel workbook")
    elif replacement_kind == "empty":
        _price(new, empty=True)
    _registry(tmp_path, [_replacement(old)])
    paths = [("mail", old)]
    assert active_source_paths(tmp_path, paths) == paths


@pytest.mark.parametrize("bad_registry", [
    "not JSON", "[]", "null", '{"schema_version": 2, "replacements": []}',
    '{"schema_version": true, "replacements": []}',
    '{"schema_version": 1, "replacements": {}}',
    '{"schema_version": 1, "replacements": [null]}',
])
def test_invalid_or_unknown_registry_preserves_original_sources(tmp_path, bad_registry):
    old = _price(tmp_path / "mail.xlsx")
    (tmp_path / "price_links.json").write_text(bad_registry, encoding="utf-8")
    paths = [("mail", old)]
    assert active_source_paths(tmp_path, paths) == paths


@pytest.mark.parametrize("schema", [2, True, "1", None])
def test_unsupported_schema_cannot_retire_an_otherwise_proven_old_copy(tmp_path, schema):
    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    _registry(tmp_path, [_replacement(old)], schema_version=schema)
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths


def test_one_bad_successor_keeps_only_its_own_old_source_available(tmp_path):
    mail = _price(tmp_path / "mail.xlsx")
    new_mail = _price(tmp_path / "mail__simfer.xlsx")
    channel = _price(tmp_path / "channel.xlsx")
    bad_channel = tmp_path / "telegram__bt.xlsx"
    bad_channel.write_bytes(b"not a price")
    _registry(tmp_path, [_replacement(mail), _replacement(channel, "telegram__bt")])
    paths = [("mail", mail), ("channel", channel), ("mail__simfer", new_mail)]
    assert active_source_paths(tmp_path, paths) == paths[1:]


@pytest.mark.parametrize("key", ["old_slot", "new_slot"])
@pytest.mark.parametrize("unsafe", ["../outside", "..\\outside", "a/b", "a\\b", "C:outside", "..", "a..b"])
def test_registry_cannot_use_path_traversal(tmp_path, key, unsafe):
    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    entry = {**_replacement(old), key: unsafe}
    _registry(tmp_path, [entry])
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths


def test_malformed_entry_makes_entire_registry_leave_sources_available(tmp_path):
    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    _registry(tmp_path, [_replacement(old), {"old_slot": "other"}])
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths


def test_conflicting_replacements_do_not_retire_old_copy(tmp_path):
    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    _registry(tmp_path, [_replacement(old), _replacement(old, "another")])
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths


def test_cached_successor_is_rechecked_after_atomic_file_replacement(tmp_path, monkeypatch):
    from content_factory.ingest import excel_price

    old = _price(tmp_path / "mail.xlsx")
    new = _price(tmp_path / "mail__simfer.xlsx")
    _registry(tmp_path, [_replacement(old)])
    paths = [("mail", old), ("mail__simfer", new)]
    original_parser = excel_price.parse_price_xlsx
    calls = []

    def counting_parser(path):
        calls.append(path)
        return original_parser(path)

    monkeypatch.setattr(excel_price, "parse_price_xlsx", counting_parser)
    assert active_source_paths(tmp_path, paths) == paths[1:]
    assert active_source_paths(tmp_path, paths) == paths[1:]
    assert len(calls) == 1

    broken = tmp_path / "next.xlsx"
    broken.write_bytes(b"broken workbook")
    broken.replace(new)
    assert active_source_paths(tmp_path, paths) == paths
    assert len(calls) == 2


def test_invalid_successor_can_recover_without_process_restart(tmp_path):
    old = _price(tmp_path / "mail.xlsx")
    new = tmp_path / "mail__simfer.xlsx"
    new.write_bytes(b"broken workbook")
    _registry(tmp_path, [_replacement(old)])
    paths = [("mail", old), ("mail__simfer", new)]
    assert active_source_paths(tmp_path, paths) == paths
    _price(new)
    assert active_source_paths(tmp_path, paths) == paths[1:]


def test_symlink_successor_outside_price_directory_cannot_retire_old(tmp_path):
    directory = tmp_path / "prices"
    directory.mkdir()
    old = _price(directory / "mail.xlsx")
    outside = _price(tmp_path / "outside.xlsx")
    try:
        (directory / "mail__simfer.xlsx").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    _registry(directory, [_replacement(old)])
    paths = [("mail", old)]
    assert active_source_paths(directory, paths) == paths


def test_missing_registry_and_metadata_are_optional(tmp_path):
    old = _price(tmp_path / "mail.xlsx")
    paths = [("mail", old)]
    assert active_source_paths(tmp_path, paths) == paths
    assert source_refresh_metadata(tmp_path) == {}


@pytest.mark.parametrize("contents", ["bad JSON", "[]", "null", '{"sources": {"bt": {"status": "fresh"}}}'])
def test_refresh_metadata_accepts_only_json_objects(tmp_path, contents):
    (tmp_path / "source_refresh.json").write_text(contents, encoding="utf-8")
    expected = {"sources": {"bt": {"status": "fresh"}}} if contents.startswith('{') else {}
    assert source_refresh_metadata(tmp_path) == expected
