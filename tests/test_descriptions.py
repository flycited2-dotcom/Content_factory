import json
from content_factory.content.descriptions import load_descriptions


def test_load_descriptions_maps_key_to_text(tmp_path):
    (tmp_path / "a.txt").write_text("Текст A", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"src|brand|series": "a.txt", "src|x|y": "missing.txt"}, ensure_ascii=False),
        encoding="utf-8")
    out = load_descriptions(tmp_path / "manifest.json")
    assert out == {"src|brand|series": "Текст A"}    # отсутствующий файл пропущен


def test_load_descriptions_missing_manifest():
    assert load_descriptions("/no/such/manifest.json") == {}
