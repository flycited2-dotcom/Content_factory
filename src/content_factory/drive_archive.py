"""Mirror validated Avito content to Drive; VPS files remain the working archive.

Uses the existing photoagent OAuth ``drive.file`` grant. Credentials and the
folder ID live outside the repository. An upload is skipped only when its SHA-256
and human-readable filename both match the remote metadata.
"""
from __future__ import annotations

import argparse
import io
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
ARTICLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9#._-]*$")


def archive_name(manifest: dict, kind: str) -> str:
    article = str(manifest["article"])
    if not ARTICLE.fullmatch(article):
        raise ValueError("unsafe article")
    title = re.sub(r"[^\w .()#-]+", " ", str(manifest.get("name") or ""),
                   flags=re.UNICODE).strip()
    title = re.sub(r"\s+", " ", title)[:90].strip()
    suffix = ".json" if kind == "manifest" else ".png"
    return f"{article} — {title or 'товар'} — {kind}{suffix}"


def content_files(content_dir: Path):
    """Only complete, internally consistent archives are mirrored."""
    for directory in sorted(content_dir.iterdir() if content_dir.is_dir() else []):
        if directory.is_symlink() or not directory.is_dir() or not ARTICLE.fullmatch(directory.name):
            continue
        try:
            manifest_path = directory / "content.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or manifest.get("article") != directory.name:
                continue
            paths = {
                "card": directory / manifest["card"],
                "original": directory / manifest["original"],
                "manifest": manifest_path,
            }
            if any(path.parent != directory or path.is_symlink() or not path.is_file() or
                   path.stat().st_size < 100 for path in paths.values()):
                continue
            if any(not valid_png(paths[kind].read_bytes()) for kind in ("card", "original")):
                continue
            for kind, path in paths.items():
                yield manifest, kind, path
        except (OSError, ValueError, KeyError, TypeError):
            continue


class DriveArchive:
    def __init__(self, token_path: Path, http: httpx.Client | None = None):
        self.token = json.loads(token_path.read_text(encoding="utf-8"))
        self.http = http or httpx.Client(timeout=120)
        self.access_token = ""

    def authenticate(self):
        response = self.http.post(
            self.token.get("token_uri") or "https://oauth2.googleapis.com/token",
            data={"grant_type": "refresh_token",
                  "refresh_token": self.token["refresh_token"],
                  "client_id": self.token["client_id"],
                  "client_secret": self.token["client_secret"]},
        )
        response.raise_for_status()
        self.access_token = response.json()["access_token"]

    def _request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.access_token}"
        response = self.http.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def find_or_create_folder(self, title: str) -> dict:
        rows = self._request(
            "GET", f"{API}/files", params={
                "q": f"name = '{title.replace(chr(39), chr(92) + chr(39))}' "
                     f"and mimeType = '{FOLDER_MIME}' and trashed = false",
                "fields": "files(id,name,webViewLink),nextPageToken", "pageSize": 100,
            })["files"]
        if rows:
            return rows[0]
        return self._request("POST", f"{API}/files",
                             params={"fields": "id,name,webViewLink"},
                             json={"name": title, "mimeType": FOLDER_MIME})

    def list_files(self, folder_id: str) -> dict[tuple[str, str], dict]:
        files = {}
        page = None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false",
                      "fields": "nextPageToken,files(id,name,appProperties)",
                      "pageSize": 1000}
            if page:
                params["pageToken"] = page
            data = self._request("GET", f"{API}/files", params=params)
            for row in data.get("files", []):
                props = row.get("appProperties") or {}
                key = (props.get("article"), props.get("kind"))
                if all(key):
                    if key in files:
                        raise ValueError(f"duplicate Drive archive file: {key}")
                    files[key] = row
            page = data.get("nextPageToken")
            if not page:
                return files

    def download(self, file_id: str) -> bytes:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        response = self.http.get(f"{API}/files/{file_id}",
                                 params={"alt": "media"}, headers=headers)
        response.raise_for_status()
        return response.content

    def upload(self, folder_id: str, manifest: dict, kind: str, path: Path,
               previous: dict | None = None) -> dict:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        name = archive_name(manifest, kind)
        if (previous and previous.get("name") == name and
                (previous.get("appProperties") or {}).get("sha256") == digest):
            return {"id": previous["id"], "skipped": True}
        meta = {"name": name, "appProperties": {
            "article": manifest["article"], "kind": kind, "sha256": digest,
            "brand": str(manifest.get("brand") or "")[:100],
            "model": str(manifest.get("model") or "")[:100]}}
        if not previous:
            meta["parents"] = [folder_id]
        boundary = "codex-" + uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                .encode() + json.dumps(meta, ensure_ascii=False).encode() +
                f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() +
                data + f"\r\n--{boundary}--\r\n".encode())
        url = f"{UPLOAD_API}/files" + (f"/{previous['id']}" if previous else "")
        result = self._request("PATCH" if previous else "POST", url,
                               params={"uploadType": "multipart",
                                       "fields": "id,name,appProperties,webViewLink"},
                               headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                               content=body)
        if result.get("appProperties", {}).get("sha256") != digest:
            raise RuntimeError(f"Drive did not confirm archive hash for {manifest['article']} {kind}")
        result["skipped"] = False
        return result

    def share_reader(self, folder_id: str, email: str):
        permissions = self._request("GET", f"{API}/files/{folder_id}/permissions",
                                    params={"fields": "permissions(emailAddress,role)"})
        if any(p.get("emailAddress", "").casefold() == email.casefold()
               for p in permissions.get("permissions", [])):
            return
        self._request("POST", f"{API}/files/{folder_id}/permissions",
                      params={"sendNotificationEmail": "false"},
                      json={"type": "user", "role": "reader", "emailAddress": email})


def _write_state(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _verified_download(drive: DriveArchive, row: dict) -> bytes:
    digest = (row.get("appProperties") or {}).get("sha256") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Drive file has no valid SHA-256")
    data = drive.download(row["id"])
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Drive download SHA-256 mismatch")
    return data


def valid_png(data: bytes) -> bool:
    if len(data) < 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or min(image.size) < 100:
                return False
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def restore_missing(content_dir: Path, token_path: Path, sync_state: Path,
                    restore_state: Path, items: list, drive: DriveArchive | None = None) -> dict:
    """Recover exact-article images before new generation; fail closed on lookup errors.

    ``items`` are current, supplier-verified new/preview ExcelItems. Only a
    fully verified three-file Drive set can replace a missing/incomplete folder.
    Old local content is kept as a hidden backup when recovery replaces it.
    """
    report = {"checked_at": time.time(), "lookup_ok": False, "restored": 0,
              "local": 0, "remote_missing": 0, "deferred": [], "errors": []}
    try:
        state = json.loads(sync_state.read_text(encoding="utf-8"))
        folder_id = state["folder_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", folder_id):
            raise ValueError("invalid Drive folder ID")
        drive = drive or DriveArchive(token_path, httpx.Client(timeout=20))
        drive.authenticate()
        remote = drive.list_files(folder_id)
        report["lookup_ok"] = True
        content_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            article = item.key.split("|", 1)[1]
            if not ARTICLE.fullmatch(article):
                report["deferred"].append(item.key)
                report["errors"].append(f"{article}: unsafe article")
                continue
            target = content_dir / article
            if target.is_symlink():
                report["deferred"].append(item.key)
                report["errors"].append(f"{article}: archive directory is a symbolic link")
                continue
            current = target / "content.json"
            try:
                if current.is_file():
                    local = json.loads(current.read_text(encoding="utf-8"))
                    if (local.get("article") == article and
                            all(local.get(k) == getattr(item, k) for k in
                                ("brand", "model", "name", "card_mode")) and
                            all((target / name).is_file() and
                                valid_png((target / name).read_bytes())
                                for name in ("card.png", "original.png"))):
                        report["local"] += 1
                        continue
            except (OSError, ValueError, TypeError):
                pass
            rows = {kind: remote.get((article, kind)) for kind in
                    ("manifest", "card", "original")}
            if not any(rows.values()):
                if target.exists():
                    report["deferred"].append(item.key)
                    report["errors"].append(f"{article}: incomplete local archive and no Drive copy")
                else:
                    report["remote_missing"] += 1
                continue
            if not all(rows.values()):
                report["deferred"].append(item.key)
                report["errors"].append(f"{article}: incomplete Drive archive")
                continue
            stage = content_dir / f".restore-{article}-{uuid.uuid4().hex}"
            try:
                raw = _verified_download(drive, rows["manifest"])
                manifest = json.loads(raw.decode("utf-8"))
                if (manifest.get("schema_version") != 1 or
                        manifest.get("article") != article or
                        any(manifest.get(k) != getattr(item, k) for k in
                            ("brand", "model", "name", "card_mode")) or
                        manifest.get("card") != "card.png" or
                        manifest.get("original") != "original.png" or
                        (manifest.get("card_text_audit") or {}).get("passed") is not True or
                        (manifest.get("evidence") or {}).get("exact_model") is not True):
                    raise ValueError("archived identity or validation differs from current item")
                images = {kind: _verified_download(drive, rows[kind]) for kind in
                          ("card", "original")}
                for kind, data in images.items():
                    if not valid_png(data):
                        raise ValueError(f"{kind}: invalid PNG")
                stage.mkdir()
                (stage / "content.json").write_bytes(raw)
                for kind, data in images.items():
                    (stage / f"{kind}.png").write_bytes(data)
                if target.exists():
                    backup = content_dir / f".restore-backup-{article}-{uuid.uuid4().hex}"
                    os.replace(target, backup)
                    try:
                        os.replace(stage, target)
                    except OSError:
                        os.replace(backup, target)
                        raise
                else:
                    os.replace(stage, target)
                report["restored"] += 1
            except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
                report["deferred"].append(item.key)
                report["errors"].append(f"{article}: {type(exc).__name__}: {exc}")
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        report["errors"].append(f"Drive lookup unavailable: {type(exc).__name__}: {exc}")
        report["deferred"] = [item.key for item in items if item.status == "new"]
    _write_state(restore_state, report)
    return report

def sync(content_dir: Path, token_path: Path, state_path: Path,
         folder_title="Avito — архив карточек (Content Завод)",
         reader_email="") -> dict:
    drive = DriveArchive(token_path)
    drive.authenticate()
    folder = drive.find_or_create_folder(folder_title)
    folder_id = folder["id"]
    if reader_email:
        drive.share_reader(folder_id, reader_email)
    existing = drive.list_files(folder_id)
    uploaded = skipped = 0
    articles = set()
    for manifest, kind, path in content_files(content_dir):
        key = (manifest["article"], kind)
        result = drive.upload(folder_id, manifest, kind, path, existing.get(key))
        articles.add(manifest["article"])
        skipped += int(result["skipped"])
        uploaded += int(not result["skipped"])
    remote_articles = {key[0] for key in existing if key[1] == "manifest"}
    report = {"finished_at": time.time(), "folder_id": folder_id,
              "url": f"https://drive.google.com/drive/folders/{folder_id}",
              "articles": len(remote_articles | articles),
              "mirrored_articles": len(articles),
              "retained_remote_articles": len(remote_articles - articles),
              "uploaded": uploaded, "unchanged": skipped}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, state_path)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-dir", type=Path, default=Path("/opt/avito-ready-price/content"))
    parser.add_argument("--token", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/opt/content-factory/state/avito-drive-sync.json"))
    parser.add_argument("--reader-email", default="")
    args = parser.parse_args()
    print(json.dumps(sync(args.content_dir, args.token, args.state,
                          reader_email=args.reader_email), ensure_ascii=False))


if __name__ == "__main__":
    main()
