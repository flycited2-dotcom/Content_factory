"""Explicit copy -> verify -> runtime test -> dry-run prune -> apply CLI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from content_factory.archive_s3 import EXTENSIONS, ArchiveError, ObjectArchive, Settings, RcloneBackend, recover_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('copy', 'verify', 'prune', 'restore', 'restore-all', 'backup', 'recover-index'))
    parser.add_argument('--root', required=True)
    parser.add_argument('--config')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-runtime-ready', action='store_true')
    parser.add_argument('--filename')
    parser.add_argument('--target')
    parser.add_argument('--snapshot-key')
    parser.add_argument('--backend', choices=('rclone', 'boto3'), default='rclone',
                        help='Optional boto3 uses existing system package; no install needed')
    args = parser.parse_args()
    settings = Settings.load(args.root, args.config, force=True)
    if settings is None:
        parser.error('Archive config required')
    backend = None
    if args.backend == 'boto3':
        from content_factory.archive_s3_boto import Boto3Backend
        backend = Boto3Backend(settings)
    if args.action == 'recover-index':
        print(json.dumps(recover_manifest(settings, backend or RcloneBackend(settings), args.snapshot_key)))
        return
    store = ObjectArchive(settings, backend)
    if args.action == 'copy':
        copied = skipped = empty_retained = 0
        for day in sorted(Path(args.root).iterdir()):
            if not day.is_dir() or day.is_symlink():
                continue
            for path in sorted(day.iterdir()):
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                    continue
                if path.stat().st_size == 0:
                    empty_retained += 1
                    continue
                if store.put_file(path, day.name + '/' + path.name):
                    copied += 1
                else:
                    skipped += 1
                if (copied + skipped) % 100 == 0:
                    print(json.dumps({'progress': 'copy', 'copied': copied, 'skipped': skipped}), flush=True)
        result = {'copied': copied, 'skipped': skipped, 'empty_files_retained_locally': empty_retained, 'manifest_entries': len(store.entries())}
        result['manifest_backup'] = store.backup_manifest()
    elif args.action == 'verify':
        result = store.verify_all()
        if not result['errors']:
            result['manifest_backup'] = store.backup_manifest()
    elif args.action == 'backup':
        result = store.backup_manifest(force=True)
    elif args.action == 'restore-all':
        result = store.restore_all(args.apply)
    elif args.action == 'restore':
        if not args.filename or not args.target:
            parser.error('restore requires --filename and --target')
        result = {'restored': store.restore_to(args.filename, args.target)}
    else:
        result = store.prune_verified(args.apply, args.confirm_runtime_ready)
    print(json.dumps(result, ensure_ascii=False))
    if result.get('errors'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
