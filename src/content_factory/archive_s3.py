"""Verified object archive; opt-in, no FUSE and no persistent image cache.

The small local manifest is required for lookup. Credentials stay in rclone's
existing protected configuration and never appear in command arguments/logs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

CONFIG_NAME = '.s3-archive.json'
EXTENSIONS = {'.png', '.jpg', '.jpeg'}


class ArchiveError(RuntimeError):
    pass


class ArchiveConflict(ArchiveError):
    pass


def _relative(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or len(p.parts) != 2 or any(x in ('', '.', '..') for x in p.parts):
        raise ArchiveError('Archive path must be date/filename')
    try:
        parsed = date.fromisoformat(p.parts[0])
        if parsed.isoformat() != p.parts[0]:
            raise ValueError('noncanonical date')
    except ValueError as exc:
        raise ArchiveError('Invalid archive date') from exc
    if '\\' in value or p.suffix.lower() not in EXTENSIONS:
        raise ArchiveError('Invalid archive filename')
    return p.as_posix()


def file_digest(path: Path) -> tuple[int, str, os.stat_result]:
    """Hash one regular, stable, non-symlink file without reading it to memory."""
    if path.is_symlink() or not path.is_file():
        raise ArchiveError('Archive source is not a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        sha = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            sha.update(chunk)
            size += len(chunk)
        after = os.fstat(handle.fileno())
    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise ArchiveError('Archive source changed while hashing')
    return size, sha.hexdigest(), after


@dataclass(frozen=True)
class Settings:
    root: Path
    config_path: Path
    mode: str
    remote: str
    manifest: Path
    rclone_config: Path
    rclone_binary: str = '/usr/bin/rclone'
    max_file_bytes: int = 128 * 1024 * 1024
    min_free_bytes: int = 256 * 1024 * 1024
    operation_timeout: int = 240
    bwlimit: str = '2M'
    read_bwlimit: str = '0'

    @classmethod
    def load(cls, root, config_path=None, force=False):
        root = Path(root).resolve()
        config_path = Path(config_path or root / CONFIG_NAME).resolve()
        if not config_path.is_file():
            return None
        data = json.loads(config_path.read_text(encoding='utf-8'))
        mode = data.get('mode', 'off')
        if mode not in ('off', 'remote'):
            raise ArchiveError('Archive mode must be off or remote')
        if mode == 'off' and not force:
            return None
        remote = data.get('remote', '').rstrip('/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+:[^\r\n]+', remote) or '..' in PurePosixPath(remote.split(':', 1)[1]).parts:
            raise ArchiveError('Invalid archive remote target')
        manifest = Path(data.get('manifest', str(root.parent / 'state' / 'archive-s3.sqlite3')))
        if not manifest.is_absolute():
            raise ArchiveError('Manifest path must be absolute')
        settings = cls(root, config_path, mode, remote, manifest,
                       Path(data.get('rclone_config', '/root/.config/rclone/rclone.conf')),
                       data.get('rclone_binary', '/usr/bin/rclone'),
                       int(data.get('max_file_bytes', 128 * 1024 * 1024)),
                       int(data.get('min_free_bytes', 256 * 1024 * 1024)),
                       int(data.get('operation_timeout', 240)), data.get('bwlimit', '2M'),
                       data.get('read_bwlimit', '0'))
        if settings.max_file_bytes <= 0 or settings.min_free_bytes < 0 or settings.operation_timeout <= 0:
            raise ArchiveError('Invalid archive resource limits')
        return settings


class RcloneBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _args(self, command, *args, reading=False):
        s = self.settings
        return [s.rclone_binary, command, *map(str, args), '--config', str(s.rclone_config),
                '--contimeout', '10s', '--timeout', '30s', '--retries', '2',
                '--low-level-retries', '2', '--transfers', '1', '--checkers', '1',
                '--bwlimit', s.read_bwlimit if reading else s.bwlimit,
                '--stats', '0', '--log-level', 'ERROR']

    def _target(self, key):
        if not (key.startswith('_manifests/') and re.fullmatch(r'_manifests/[0-9a-f-]+\.sqlite3', key)):
            key = _relative(key)
        return self.settings.remote + '/' + key

    def digest(self, key):
        # rclone cat on an absent S3 key can return success with zero bytes.
        # lsjson --stat represents that same absent path as an IsDir sentinel.
        try:
            result = subprocess.run(self._args('lsjson', self._target(key), '--stat', reading=True),
                                    capture_output=True, timeout=self.settings.operation_timeout)
            if result.returncode in (3, 4):
                return None
            if result.returncode:
                raise ArchiveError('rclone stat failed')
            info = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise ArchiveError('rclone stat failed') from exc
        if info.get('IsDir') or info.get('Size', -1) < 0:
            return None
        if info['Size'] > self.settings.max_file_bytes:
            raise ArchiveError('Remote object exceeds archive size limit')
        args = self._args('cat', self._target(key), reading=True)
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        timer = threading.Timer(self.settings.operation_timeout, process.kill)
        timer.start()
        size, sha = 0, hashlib.sha256()
        try:
            while chunk := process.stdout.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_file_bytes:
                    process.kill()
                    raise ArchiveError('Remote object exceeds archive size limit')
                sha.update(chunk)
            code = process.wait()
            if code in (3, 4) and size == 0:
                return None
            if code:
                raise ArchiveError('rclone read failed; retry without discarding local data')
            if size != info['Size']:
                raise ArchiveError('Remote object changed during read')
            return size, sha.hexdigest()
        finally:
            timer.cancel()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()

    def _copy(self, source, target, reading=False):
        try:
            result = subprocess.run(self._args('copyto', source, target, '--ignore-times', reading=reading),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=self.settings.operation_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ArchiveError('rclone transfer failed; local data retained') from exc
        if result.returncode:
            raise ArchiveError('rclone transfer failed; local data retained')

    def upload(self, key, source):
        try:
            result = subprocess.run(self._args('copyto', source, self._target(key),
                                              '--ignore-times', '--s3-acl', 'private'),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=self.settings.operation_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ArchiveError('rclone upload failed; local data retained') from exc
        if result.returncode:
            raise ArchiveError('rclone upload failed; local data retained')

    def download(self, key, target):
        self._copy(self._target(key), target, reading=True)


class ObjectArchive:
    def __init__(self, settings: Settings, backend=None):
        self.settings = settings
        self.backend = backend or RcloneBackend(settings)
        if settings.mode == 'remote':
            # A missing index after prune must never look like an empty archive:
            # callers could otherwise silently start a fresh paid generation.
            if not settings.manifest.is_file() or settings.manifest.stat().st_size == 0:
                raise ArchiveError('Active archive manifest missing; restore verified backup')
            try:
                with self._db() as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ArchiveError('Active archive manifest is corrupt')
                    row = db.execute("SELECT value FROM meta WHERE key='remote'").fetchone()
                    db.execute('SELECT relative,sha256 FROM files LIMIT 1').fetchone()
                    if not row or row[0] != settings.remote:
                        raise ArchiveConflict('Active manifest remote binding invalid')
            except sqlite3.DatabaseError as exc:
                raise ArchiveError('Active archive manifest is invalid') from exc
        settings.manifest.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(settings.manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    relative TEXT PRIMARY KEY, filename TEXT NOT NULL, day TEXT NOT NULL,
                    bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, source_dev TEXT NOT NULL,
                    source_inode TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL,
                    verified_at REAL NOT NULL, prune_verified INTEGER NOT NULL DEFAULT 0,
                    local_removed_at REAL
                );
                CREATE INDEX IF NOT EXISTS files_name ON files(filename, day DESC);
            ''')
            row = db.execute("SELECT value FROM meta WHERE key='remote'").fetchone()
            if row and row[0] != settings.remote:
                raise ArchiveConflict('Manifest belongs to a different remote target')
            db.execute("INSERT OR IGNORE INTO meta VALUES ('remote',?)", (settings.remote,))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('generation','0')")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.settings.manifest, timeout=300)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def entries(self):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM files ORDER BY relative')]

    def knows(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return False
        with self._db() as db:
            return db.execute('SELECT 1 FROM files WHERE filename=? LIMIT 1', (filename,)).fetchone() is not None

    def _check_size(self, size):
        if not 0 < size <= self.settings.max_file_bytes:
            raise ArchiveError('Archive image outside configured size limits')

    @contextmanager
    def local_write(self):
        """Serialize fallback archive copies with an explicit prune operation."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            yield

    def backup_manifest(self, force=False):
        """Upload an immutable, internally consistent SQLite snapshot, never DB/WAL cp."""
        with self._db() as db:
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('generation','backup_generation','backup_record')"))
        if not force and meta.get('generation') == meta.get('backup_generation') and meta.get('backup_record'):
            return dict(json.loads(meta['backup_record']), unchanged=True)
        fd, name = tempfile.mkstemp(prefix='.archive-manifest-', suffix='.sqlite3',
                                    dir=self.settings.manifest.parent)
        os.close(fd)
        snapshot = Path(name)
        try:
            with self._db() as source:
                target = sqlite3.connect(snapshot)
                try:
                    source.backup(target)
                    generation = int(target.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ArchiveError('Manifest snapshot failed integrity check')
                finally:
                    target.close()
            expected = file_digest(snapshot)[:2]
            key = '_manifests/' + f'{generation:020d}-' + uuid.uuid4().hex + '.sqlite3'
            self.backend.upload(key, snapshot)
            if self.backend.digest(key) != expected:
                raise ArchiveError('Manifest backup failed SHA-256 verification')
            record = {'key': key, 'generation': generation, 'bytes': expected[0], 'sha256': expected[1]}
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('backup_generation',?)", (str(generation),))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('backup_record',?)", (json.dumps(record),))
            return record
        finally:
            snapshot.unlink(missing_ok=True)

    def put_file(self, source, relative):
        source, relative = Path(source), _relative(relative)
        size, digest, source_stat = file_digest(source)
        self._check_size(size)
        # A writer lock spans upload + verification; other readers use WAL.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM files WHERE relative=?', (relative,)).fetchone()
            if row:
                if (row['bytes'], row['sha256']) != (size, digest):
                    raise ArchiveConflict('Existing archive name has different content')
                return False
            remote = self.backend.digest(relative)
            if remote is not None and remote != (size, digest):
                raise ArchiveConflict('Remote archive object differs; refusing overwrite')
            if remote is None:
                self.backend.upload(relative, source)
                if self.backend.digest(relative) != (size, digest):
                    raise ArchiveError('Uploaded object failed SHA-256 verification')
            now_size, now_digest, after = file_digest(source)
            if (now_size, now_digest, after.st_dev, after.st_ino, after.st_mtime_ns) != (size, digest, source_stat.st_dev, source_stat.st_ino, source_stat.st_mtime_ns):
                raise ArchiveError('Source changed during archive upload; local file retained')
            day, filename = relative.split('/', 1)
            db.execute('INSERT INTO files(relative,filename,day,bytes,sha256,source_dev,source_inode,source_mtime_ns,verified_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (relative, filename, day, size, digest, str(after.st_dev), str(after.st_ino), after.st_mtime_ns, time.time()))
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        return True

    def restore_to(self, filename, target):
        """Return False only when unknown. Known-but-unavailable must not regenerate."""
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return False
        with self._db() as db:
            row = db.execute('SELECT * FROM files WHERE filename=? ORDER BY day DESC LIMIT 1', (filename,)).fetchone()
        if row is None:
            return False
        return self._restore_row(row, target)

    def restore_all(self, apply=False):
        """Rehydrate exact date/name keys before rollback to the former local reader."""
        missing = []
        for row in self.entries():
            target = self.settings.root / _relative(row['relative'])
            if not target.resolve().is_relative_to(self.settings.root):
                raise ArchiveError('Restore path escapes archive root')
            if target.is_file():
                if file_digest(target)[:2] != (row['bytes'], row['sha256']):
                    raise ArchiveConflict('Existing archive file differs; refusing overwrite')
            else:
                missing.append((row, target))
        needed = sum(row['bytes'] for row, target in missing)
        if shutil.disk_usage(self.settings.root).free < needed + self.settings.min_free_bytes:
            raise ArchiveError('Insufficient free disk for complete rollback restore')
        result = {'missing': len(missing), 'required_bytes': needed, 'restored': 0}
        if apply:
            for row, target in missing:
                self._restore_row(row, target)
                result['restored'] += 1
        return result

    def _restore_row(self, row, target):
        target = Path(target)
        expected = (row['bytes'], row['sha256'])
        self._check_size(row['bytes'])
        if target.exists():
            if file_digest(target)[:2] != expected:
                raise ArchiveConflict('Existing restored output differs; refusing overwrite')
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < row['bytes'] + self.settings.min_free_bytes:
            raise ArchiveError('Insufficient local space for restored image')
        fd, temp_name = tempfile.mkstemp(prefix='.archive-download-', suffix='.tmp', dir=target.parent)
        os.close(fd)
        temp = Path(temp_name)
        try:
            self.backend.download(row['relative'], temp)
            if file_digest(temp)[:2] != expected:
                raise ArchiveError('Restored object failed SHA-256 verification')
            # Hard-link installation is atomic and never overwrites another worker.
            try:
                os.link(temp, target)
            except FileExistsError:
                if file_digest(target)[:2] != expected:
                    raise ArchiveConflict('Concurrent restored output differs')
            return True
        finally:
            temp.unlink(missing_ok=True)

    def verify_all(self):
        entries = self.entries()
        errors = []
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('full_verify_ok','0')")
        for row in entries:
            try:
                if self.backend.digest(row['relative']) != (row['bytes'], row['sha256']):
                    raise ArchiveError('Remote verification mismatch')
                local = self.settings.root / row['relative']
                if local.exists() and file_digest(local)[:2] != (row['bytes'], row['sha256']):
                    raise ArchiveError('Local verification mismatch')
                with self._db() as db:
                    db.execute('UPDATE files SET prune_verified=1, verified_at=? WHERE relative=? AND sha256=?', (time.time(), row['relative'], row['sha256']))
            except (ArchiveError, OSError) as exc:
                errors.append({'relative': row['relative'], 'error': type(exc).__name__})
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('full_verify_ok',?)", ('0' if errors else '1',))
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        return {'verified': len(entries) - len(errors), 'errors': errors}

    def prune_verified(self, apply=False, runtime_ready=False):
        """Explicit post-deployment step, never called by the archive writer."""
        if self.settings.mode != 'remote' or not runtime_ready or self.settings.config_path != (self.settings.root / CONFIG_NAME).resolve():
            raise ArchiveError('Prune requires active root config and confirmed runtime read path')
        with self._db() as db:
            ready = db.execute("SELECT value FROM meta WHERE key='full_verify_ok'").fetchone()
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('generation','backup_generation')"))
        if not ready or ready[0] != '1':
            raise ArchiveError('Complete successful verification required before prune')
        if int(meta.get('backup_generation', '-1')) < int(meta['generation']):
            raise ArchiveError('Current manifest must have a verified remote backup before prune')
        result = {'eligible': 0, 'eligible_bytes': 0, 'removed': 0, 'removed_bytes': 0, 'skipped': 0}
        for row in self.entries():
            path = self.settings.root / _relative(row['relative'])
            # Open dates can still be generated, rewritten, or used by a live worker.
            if date.fromisoformat(row['day']) >= date.today():
                result['skipped'] += 1
                continue
            if not path.exists():
                continue
            if not path.resolve().is_relative_to(self.settings.root):
                raise ArchiveError('Prune path escapes archive root')
            size, digest, st = file_digest(path)
            if not row['prune_verified'] or st.st_nlink != 1 or (str(st.st_dev), str(st.st_ino), st.st_mtime_ns, size, digest) != (row['source_dev'], row['source_inode'], row['source_mtime_ns'], row['bytes'], row['sha256']):
                result['skipped'] += 1
                continue
            if self.backend.digest(row['relative']) != (size, digest):
                raise ArchiveError('Remote changed before prune; no deletion of this file')
            # Recheck identity after the remote round trip, including full hash.
            again_size, again_digest, again = file_digest(path)
            if (again_size, again_digest, again.st_dev, again.st_ino, again.st_mtime_ns) != (size, digest, st.st_dev, st.st_ino, st.st_mtime_ns):
                raise ArchiveError('Local file changed before prune')
            result['eligible'] += 1
            result['eligible_bytes'] += size
            if apply:
                with self._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('generation','backup_generation','full_verify_ok')"))
                    if current.get('full_verify_ok') != '1' or int(current.get('backup_generation', '-1')) < int(current['generation']):
                        raise ArchiveError('Manifest changed since backup; stop prune and reverify/rebackup')
                    # Moving into a private directory isolates this inode from any
                    # subsequent path replacement. Recheck the moved object, then
                    # delete only that verified inode. Never overwrite a new source.
                    quarantine_dir = self.settings.root / '.s3-prune-quarantine'
                    quarantine_dir.mkdir(mode=0o700, exist_ok=True)
                    if quarantine_dir.is_symlink():
                        raise ArchiveError('Invalid quarantine directory')
                    quarantined = quarantine_dir / (uuid.uuid4().hex + '.pending')
                    os.rename(path, quarantined)
                    try:
                        q_size, q_digest, q_st = file_digest(quarantined)
                        if (q_size, q_digest, q_st.st_dev, q_st.st_ino, q_st.st_mtime_ns, q_st.st_nlink) != (size, digest, st.st_dev, st.st_ino, st.st_mtime_ns, 1):
                            raise ArchiveError('Source replaced before quarantine; retained')
                        quarantined.unlink()
                    except BaseException:
                        # If another writer has already created path, preserve both
                        # copies and report the quarantine path for manual recovery.
                        if quarantined.exists():
                            try:
                                os.link(quarantined, path)
                                quarantined.unlink()
                            except FileExistsError:
                                pass
                        raise
                    db.execute('UPDATE files SET local_removed_at=? WHERE relative=?', (time.time(), row['relative']))
                result['removed'] += 1
                result['removed_bytes'] += size
        return result


def archive_for(root, config_path=None, force=False):
    settings = Settings.load(root, config_path, force)
    return ObjectArchive(settings) if settings is not None else None


def recover_manifest(settings, backend, key):
    """Explicit recovery only: never replace an existing local index automatically."""
    if not re.fullmatch(r'_manifests/[0-9a-f-]+\.sqlite3', key or ''):
        raise ArchiveError('Select an exact immutable manifest snapshot key')
    if any(Path(str(settings.manifest) + suffix).exists() for suffix in ('', '-wal', '-shm')):
        raise ArchiveConflict('Manifest already exists; refusing recovery overwrite')
    settings.manifest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.archive-recover-', suffix='.sqlite3', dir=settings.manifest.parent)
    os.close(fd)
    temp = Path(name)
    try:
        expected = backend.digest(key)
        if expected is None:
            raise ArchiveError('Manifest snapshot unavailable')
        backend.download(key, temp)
        if file_digest(temp)[:2] != expected:
            raise ArchiveError('Recovered manifest SHA-256 mismatch')
        db = sqlite3.connect(temp.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ArchiveError('Recovered manifest integrity check failed')
            meta = dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('remote') != settings.remote:
                raise ArchiveConflict('Snapshot remote target differs')
            count = db.execute('SELECT COUNT(*) FROM files').fetchone()[0]
            generation = int(meta['generation'])
        except (sqlite3.DatabaseError, ValueError, KeyError):
            raise ArchiveError('Invalid recovered manifest schema') from None
        finally:
            db.close()
        os.link(temp, settings.manifest)
        return {'recovered': count, 'generation': generation, 'sha256': expected[1]}
    finally:
        temp.unlink(missing_ok=True)
