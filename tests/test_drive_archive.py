import json
import hashlib
import io
import random
from types import SimpleNamespace
from PIL import Image

from content_factory.drive_archive import archive_name, content_files, restore_missing


def test_archive_names_and_complete_content_only(tmp_path):
    folder = tmp_path / "00-00002606"
    folder.mkdir()
    manifest = {"schema_version": 1, "article": folder.name,
                "name": "Аэрогриль Midea MAF-TN40D", "brand": "Midea",
                "model": "MAF-TN40D", "card": "card.png",
                "original": "original.png"}
    (folder / "content.json").write_text(json.dumps(manifest), encoding="utf-8")
    Image.new("RGB", (128, 128), "blue").save(folder / "card.png", compress_level=0)
    assert list(content_files(tmp_path)) == []
    Image.new("RGB", (128, 128), "green").save(folder / "original.png", compress_level=0)
    rows = list(content_files(tmp_path))
    assert [kind for _, kind, _ in rows] == ["card", "original", "manifest"]
    assert archive_name(manifest, "card") == "00-00002606 — Аэрогриль Midea MAF-TN40D — card.png"


def test_archive_rejects_path_traversal(tmp_path):
    folder = tmp_path / "A-1"
    folder.mkdir()
    (tmp_path / "outside.png").write_bytes(b"x" * 200)
    (folder / "card.png").write_bytes(b"x" * 200)
    (folder / "content.json").write_text(json.dumps({
        "schema_version": 1, "article": "A-1", "name": "Item",
        "card": "card.png", "original": "../outside.png"}), encoding="utf-8")
    assert list(content_files(tmp_path)) == []


def _restore_fixture(tmp_path):
    article = "A-1"
    item = SimpleNamespace(key=f"ready-price|{article}", brand="BQ", model="X1",
                           name="Аэрогриль BQ X1", card_mode="kbt", status="new")
    manifest = {"schema_version": 1, "article": article, "brand": item.brand,
                "model": item.model, "name": item.name, "card_mode": item.card_mode,
                "card": "card.png", "original": "original.png",
                "card_text_audit": {"passed": True},
                "evidence": {"exact_model": True}}
    def png(seed):
        image = Image.frombytes("RGB", (128, 128),
                                random.Random(seed).randbytes(128 * 128 * 3))
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    payloads = {"manifest": json.dumps(manifest).encode(),
                "card": png(1), "original": png(2)}
    rows = {(article, kind): {"id": kind, "appProperties": {
        "sha256": hashlib.sha256(data).hexdigest()}}
        for kind, data in payloads.items()}

    class FakeDrive:
        def authenticate(self):
            pass

        def list_files(self, folder_id):
            assert folder_id == "ValidFolderId123"
            return rows

        def download(self, file_id):
            return payloads[file_id]

    sync_state = tmp_path / "sync.json"
    sync_state.write_text(json.dumps({"folder_id": "ValidFolderId123"}))
    return item, payloads, rows, FakeDrive(), sync_state


def test_restore_exact_archived_item_and_keep_old_partial_copy(tmp_path):
    item, payloads, _, drive, sync_state = _restore_fixture(tmp_path)
    content = tmp_path / "content"
    partial = content / "A-1"
    partial.mkdir(parents=True)
    (partial / "card.png").write_bytes(b"old partial")
    report = restore_missing(content, tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert report["lookup_ok"] and report["restored"] == 1, report
    assert (partial / "card.png").read_bytes() == payloads["card"]
    assert (partial / "original.png").read_bytes() == payloads["original"]
    assert len(list(content.glob(".restore-backup-A-1-*"))) == 1


def test_restore_defers_new_job_on_bad_hash_or_lookup_failure(tmp_path):
    item, payloads, _, drive, sync_state = _restore_fixture(tmp_path)
    payloads["card"] = b"\x89PNG\r\n\x1a\n" + b"tampered" * 200
    content = tmp_path / "content"
    report = restore_missing(content, tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert report["restored"] == 0 and report["deferred"] == [item.key]
    assert not (content / "A-1").exists()
    sync_state.unlink()
    report = restore_missing(content, tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert not report["lookup_ok"] and report["deferred"] == [item.key]


def test_restore_does_not_attach_another_model_or_start_over_partial_remote(tmp_path):
    item, _, rows, drive, sync_state = _restore_fixture(tmp_path)
    item.model = "another-model"
    report = restore_missing(tmp_path / "content", tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert report["deferred"] == [item.key] and not report["restored"]
    del rows[("A-1", "card")]
    report = restore_missing(tmp_path / "content", tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert report["deferred"] == [item.key] and "incomplete Drive" in report["errors"][0]
    rows.clear()
    report = restore_missing(tmp_path / "content", tmp_path / "token", sync_state,
                             tmp_path / "restore.json", [item], drive=drive)
    assert report["lookup_ok"] and report["remote_missing"] == 1
    assert report["deferred"] == []
