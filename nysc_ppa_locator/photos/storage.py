"""Private objects are opened by authorised views, never linked directly."""
from datetime import datetime, timezone
from itertools import islice
import os

from django.core.files.storage import FileSystemStorage
from storages.backends.s3 import S3Storage


PRIVATE_PHOTO_PREFIXES = ('conversation-photos/', 'reported-photos/')


def _scan_options(prefix, limit, after=None):
    if (prefix not in PRIVATE_PHOTO_PREFIXES or isinstance(limit, bool)
            or not isinstance(limit, int) or not 1 <= limit <= 5000):
        raise ValueError('Use a supported private-photo scan scope and limit.')
    if after is not None and (
        not isinstance(after, str) or not after.startswith(prefix) or len(after) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in after)
    ):
        raise ValueError('Use a supported private-photo scan scope and limit.')


class PrivateURLMixin:
    def url(self, name, *args, **kwargs):
        raise ValueError('Private photos must be opened through the authorised application endpoint.')


class PrivateFileSystemStorage(PrivateURLMixin, FileSystemStorage):
    supports_private_photo_cursor = False

    def iter_private_photo_objects(self, prefix, limit, after=None):
        """Inspect bounded flat directory entries without following any symlink.

        Invalid entries still consume the budget. A ``None`` modification time
        marks a non-file or symlink; maintenance must skip these entries.
        """
        _scan_options(prefix, limit, after)
        if after is not None:
            raise ValueError('Persistent photo cursors require object-backed storage.')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root_fd = os.open(self.location, flags)
        except FileNotFoundError:
            return
        try:
            try:
                namespace_fd = os.open(prefix[:-1], flags, dir_fd=root_fd)
            except FileNotFoundError:
                return
            try:
                with os.scandir(namespace_fd) as entries:
                    for entry in islice(entries, limit):
                        key = prefix + entry.name
                        if not entry.is_file(follow_symlinks=False):
                            yield key, None
                            continue
                        try:
                            modified = entry.stat(follow_symlinks=False).st_mtime
                        except FileNotFoundError:
                            yield key, None
                            continue
                        yield key, datetime.fromtimestamp(modified, tz=timezone.utc)
            finally:
                os.close(namespace_fd)
        finally:
            os.close(root_fd)


class PrivateS3PhotoStorage(PrivateURLMixin, S3Storage):
    supports_private_photo_cursor = True

    def iter_private_photo_objects(self, prefix, limit, after=None):
        """Use the provider's bounded collection, never an unbounded listdir."""
        _scan_options(prefix, limit, after)
        object_prefix = self._normalize_name(prefix)
        root_prefix = self.location.strip('/')
        root_prefix = root_prefix + '/' if root_prefix else ''
        scope = {'Prefix': object_prefix}
        if after is not None:
            scope['Marker'] = self._normalize_name(after)
        objects = self.bucket.objects.filter(**scope).limit(limit)
        for item in islice(objects, limit):
            key = getattr(item, 'key', None)
            if not isinstance(key, str) or not key.startswith(root_prefix):
                yield '', None
                continue
            yield key[len(root_prefix):], getattr(item, 'last_modified', None)
