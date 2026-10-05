"""Recovery must never substitute a similar model or reuse an unaudited card."""
import json

from PIL import Image
import pytest

from content_factory.archive_recovery import find_archive_recovery


def _archive(root, article="00-00000052", **overrides):
    directory = root / article
    directory.mkdir(parents=True)
    manifest = {
        "schema_version": 1, "article": article, "brand": "Samsung",
        "model": "WW11CGP44CSBLP", "name": "Стиральная машина Samsung WW11CGP44CSBLP",
        "price": 50512, "card_mode": "ready_light", "original": "original.png",
        "card": "card.png", "evidence": {"exact_model": True,
            "source_url": "https://example.test/WW11CGP44CSBLP", "features": ["11 кг"]},
        "card_text_audit": {"passed": True}, **overrides,
    }
    for filename, color in [("original.png", "blue"), ("card.png", "green")]:
        Image.new("RGB", (128, 128), color).save(directory / filename, compress_level=0)
    (directory / "content.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _find(root, **overrides):
    args = {"brand": "Samsung", "model": "WW11CGP44CSBLP", "card_mode": "ready_light", **overrides}
    return find_archive_recovery(root, **args)


def test_exact_archive_returns_original_card_and_auditable_metadata(tmp_path):
    directory = _archive(tmp_path)
    result = _find(tmp_path, expected_price=50512)
    assert result.original_path == directory / "original.png"
    assert result.card_path == directory / "card.png"
    assert result.manifest_path == directory / "content.json"
    assert result.evidence["exact_model"] is True
    assert result.name == "Стиральная машина Samsung WW11CGP44CSBLP"
    assert result.price == 50512


def test_original_photo_is_independent_of_requested_card_template(tmp_path):
    directory = _archive(tmp_path)
    result = _find(tmp_path, card_mode="kbt")
    assert result.original_path == directory / "original.png"
    assert result.card_path is None


def test_stale_price_blocks_only_finished_card_not_original(tmp_path):
    directory = _archive(tmp_path)
    result = _find(tmp_path, expected_price=54000)
    assert result.original_path == directory / "original.png"
    assert result.card_path is None
    assert _find(tmp_path).card_path == directory / "card.png"


@pytest.mark.parametrize("identity", [
    {"brand": "LG"}, {"model": "WW11CGP44CSBL"}, {"model": "WW11CGP44CSBLP-X"},
    {"model": "WW11 CGP44CSBLP"}, {"brand": ""}, {"model": ""},
])
def test_similar_or_incomplete_identity_cannot_substitute_an_archive(tmp_path, identity):
    _archive(tmp_path)
    assert _find(tmp_path, **identity) is None


def test_case_and_surrounding_whitespace_are_harmless(tmp_path):
    directory = _archive(tmp_path)
    assert _find(tmp_path, brand=" samsung ", model=" ww11cgp44csblp ").original_path == directory / "original.png"


@pytest.mark.parametrize("evidence", [None, {}, {"exact_model": False}, {"exact_model": 1},
    {"exact_model": "true"}, {"exact_model": True, "model": "ANOTHER"},
    {"exact_model": True, "brand": "LG"}])
def test_unproven_or_contradictory_model_evidence_is_rejected(tmp_path, evidence):
    _archive(tmp_path, evidence=evidence)
    assert _find(tmp_path) is None


@pytest.mark.parametrize("audit", [None, {}, {"passed": False}, {"passed": 1}, {"passed": "true"}])
def test_original_can_be_used_but_unaudited_card_cannot(tmp_path, audit):
    directory = _archive(tmp_path, card_text_audit=audit)
    result = _find(tmp_path)
    assert result.original_path == directory / "original.png"
    assert result.card_path is None


def test_duplicates_are_ambiguous_until_article_is_explicit(tmp_path):
    first = _archive(tmp_path, "A-1")
    _archive(tmp_path, "A-2")
    assert _find(tmp_path) is None
    result = _find(tmp_path, article="A-1")
    assert result.article == "A-1"
    assert result.original_path == first / "original.png"


@pytest.mark.parametrize("schema", [2, True, "1", None])
def test_unknown_manifest_schema_is_rejected(tmp_path, schema):
    _archive(tmp_path, schema_version=schema)
    assert _find(tmp_path) is None


def test_folder_article_must_match_manifest_article(tmp_path):
    directory = _archive(tmp_path)
    manifest = json.loads((directory / "content.json").read_text())
    manifest["article"] = "ANOTHER"
    (directory / "content.json").write_text(json.dumps(manifest))
    assert _find(tmp_path) is None


@pytest.mark.parametrize("unsafe", ["../outside.png", "..\\outside.png", "/outside.png", "C:\\outside.png"])
@pytest.mark.parametrize("kind", ["original", "card"])
def test_manifest_images_cannot_escape_archive_directory(tmp_path, unsafe, kind):
    directory = _archive(tmp_path, **{kind: unsafe})
    result = _find(tmp_path)
    if kind == "original":
        assert result is None
    else:
        assert result.original_path == directory / "original.png"
        assert result.card_path is None


@pytest.mark.parametrize("kind", ["original", "card"])
@pytest.mark.parametrize("bad_image", ["signature", "small", "missing", "jpeg"])
def test_full_png_validation_and_original_requirement(tmp_path, kind, bad_image):
    directory = _archive(tmp_path)
    path = directory / f"{kind}.png"
    if bad_image == "signature":
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
    elif bad_image == "small":
        Image.new("RGB", (20, 20), "blue").save(path, compress_level=0)
    elif bad_image == "jpeg":
        Image.new("RGB", (128, 128), "blue").save(path, format="JPEG")
    else:
        path.unlink()
    result = _find(tmp_path)
    if kind == "original":
        assert result is None
    else:
        assert result.original_path == directory / "original.png"
        assert result.card_path is None


def test_png_cache_invalidates_when_original_is_replaced(tmp_path):
    directory = _archive(tmp_path)
    assert _find(tmp_path) is not None
    replacement = directory / "replacement.png"
    replacement.write_bytes(b"broken")
    replacement.replace(directory / "original.png")
    assert _find(tmp_path) is None


def test_symlink_image_cannot_escape_archive(tmp_path):
    directory = _archive(tmp_path)
    outside = tmp_path / "outside.png"
    (directory / "original.png").replace(outside)
    try:
        (directory / "original.png").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    assert _find(tmp_path) is None


def test_missing_or_broken_manifest_does_not_block_other_exact_candidate(tmp_path):
    directory = _archive(tmp_path, "A-1")
    (directory / "content.json").write_text("broken JSON")
    valid = _archive(tmp_path, "A-2")
    assert _find(tmp_path).original_path == valid / "original.png"


def test_requested_article_cannot_use_path_traversal(tmp_path):
    _archive(tmp_path)
    assert _find(tmp_path, article="../00-00000052") is None
    assert _find(tmp_path, article="..\\00-00000052") is None


def test_recovery_reads_archive_without_changing_files(tmp_path):
    _archive(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert _find(tmp_path) is not None
    after = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before
