"""Optional faster CLI transport using already-installed system boto3.

The bot runtime keeps its stdlib+rclone transport. Never print loaded config.
"""
from __future__ import annotations
import configparser
import hashlib
from pathlib import Path
from content_factory.archive_s3 import ArchiveError, RcloneBackend


class Boto3Backend(RcloneBackend):
    def __init__(self, settings):
        super().__init__(settings)
        try:
            import boto3
            from botocore.config import Config
            config = configparser.ConfigParser(interpolation=None)
            config.read(settings.rclone_config)
            remote, path = settings.remote.split(':', 1)
            section = config[remote]
            if section.get('type') != 's3' or section.get('env_auth', 'false').lower() == 'true':
                raise ArchiveError('Fast transport requires explicit rclone S3 credentials')
            self.bucket, self.prefix = path.split('/', 1)
            self.client = boto3.client('s3', endpoint_url=section['endpoint'],
                region_name=section.get('region', 'spb'),
                aws_access_key_id=section['access_key_id'],
                aws_secret_access_key=section['secret_access_key'],
                config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=30,
                              max_pool_connections=1,
                              retries={'max_attempts': 3, 'mode': 'standard'},
                              s3={'addressing_style': 'path'}))
        except Exception:
            raise ArchiveError('Cannot initialize optional fast S3 transport') from None

    def _key(self, key):
        self._target(key)  # Apply the same strict date/name or manifest validation.
        return self.prefix.rstrip('/') + '/' + key

    def _read(self, key, target=None):
        body = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            body = response['Body']
            if response['ContentLength'] > self.settings.max_file_bytes:
                raise ArchiveError('Remote object exceeds archive size limit')
            size, sha = 0, hashlib.sha256()
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_file_bytes:
                    raise ArchiveError('Remote object exceeds archive size limit')
                sha.update(chunk)
                if target is not None:
                    target.write(chunk)
            if size != response['ContentLength']:
                raise ArchiveError('Incomplete remote object read')
            return size, sha.hexdigest()
        except ArchiveError:
            raise
        except Exception as exc:
            code = str(getattr(exc, 'response', {}).get('Error', {}).get('Code', ''))
            if target is None and code in ('NoSuchKey', '404', 'NotFound'):
                return None
            raise ArchiveError('S3 read failed; local data retained') from None
        finally:
            if body is not None:
                body.close()

    def digest(self, key):
        return self._read(key)

    def upload(self, key, source):
        try:
            with Path(source).open('rb') as body:
                self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=body, ACL='private')
        except Exception:
            raise ArchiveError('S3 upload failed; local data retained') from None

    def download(self, key, target):
        with Path(target).open('wb') as output:
            self._read(key, output)
