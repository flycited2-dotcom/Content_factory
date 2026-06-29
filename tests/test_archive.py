from datetime import date
from content_factory.archive import card_date, archive_card, archive_dir


def test_card_date_from_filename():
    assert card_date("lg_lg-procool-dual-inverter_2026-06-30_004808.png") == "2026-06-30"
    assert card_date("expertair-by-zilon_serii-cyclone_2026-06-23_205445.png") == "2026-06-23"


def test_card_date_none_when_absent():
    assert card_date("no_date_here.png") is None


def test_archive_card_copies_into_date_folder(tmp_path):
    src = tmp_path / "out" / "midea_msac_2026-06-30_010203.png"
    src.parent.mkdir()
    src.write_bytes(b"PNGDATA")
    root = tmp_path / "archive"
    dest, copied = archive_card(src, root)
    assert copied is True
    assert dest == root / "2026-06-30" / src.name
    assert dest.read_bytes() == b"PNGDATA"


def test_archive_card_dedup_second_call(tmp_path):
    src = tmp_path / "out" / "lg_x_2026-06-30_010203.png"
    src.parent.mkdir()
    src.write_bytes(b"A")
    root = tmp_path / "archive"
    archive_card(src, root)
    dest, copied = archive_card(src, root)        # повтор — не копируем заново
    assert copied is False and dest.exists()


def test_archive_card_fallback_date_when_no_date_in_name(tmp_path):
    src = tmp_path / "out" / "card_no_date.png"
    src.parent.mkdir()
    src.write_bytes(b"X")
    root = tmp_path / "archive"
    dest, _ = archive_card(src, root, when=date(2026, 7, 1))
    assert dest == root / "2026-07-01" / src.name


def test_archive_dir_organizes_and_skips(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "a_2026-06-30_010101.png").write_bytes(b"1")
    (out / "b_2026-06-29_020202.png").write_bytes(b"2")
    (out / "notes.txt").write_bytes(b"ignore")   # не png — пропустить
    root = tmp_path / "archive"
    archived, skipped = archive_dir(out, root)
    assert archived == 2 and skipped == 0
    assert (root / "2026-06-30" / "a_2026-06-30_010101.png").exists()
    assert (root / "2026-06-29" / "b_2026-06-29_020202.png").exists()
    # повторный прогон — всё уже там
    archived2, skipped2 = archive_dir(out, root)
    assert archived2 == 0 and skipped2 == 2
