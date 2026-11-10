"""Bounded, opt-in removal of old objects which have no retained photo record."""
from datetime import datetime, timedelta
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from photos.models import PhotoAttachment, PhotoEvidence
from photos.storage import PRIVATE_PHOTO_PREFIXES


PRIVATE_KEY = re.compile(r'^(?:conversation-photos|reported-photos)/[a-f0-9]{32}\.jpg$')
INVALID_OPTIONS = 'Choose a limit from 1 to 5000 and a minimum age of at least 168 hours.'
FAILED = 'Private photo maintenance could not complete. Remaining objects were not removed.'
CURSOR_FAILED = 'Use a private, operator-owned cursor file in a private directory.'


def _number(value):
    if isinstance(value, bool):
        raise CommandError(INVALID_OPTIONS)
    text = str(value)
    if not text.isascii() or not text.isdigit() or not 1 <= len(text) <= 10:
        raise CommandError(INVALID_OPTIONS)
    return int(text)


def _valid_marker(key, prefix):
    return (isinstance(key, str) and key.startswith(prefix) and len(key) <= 1024
            and not any(ord(char) < 32 or ord(char) == 127 for char in key))


def _read_cursor(value):
    if value is None:
        return None, {}
    try:
        path = Path(value).absolute()
        parent = path.parent.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) & 0o022):
            raise ValueError
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return path, {}
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384):
                raise ValueError
            data = json.loads(stream.read(16385))
        if (not isinstance(data, dict) or set(data) != {'version', 'after'}
                or type(data['version']) is not int or data['version'] != 1 or not isinstance(data['after'], dict)
                or not set(data['after']).issubset(PRIVATE_PHOTO_PREFIXES)):
            raise ValueError
        for prefix, key in data['after'].items():
            if key is not None and not _valid_marker(key, prefix):
                raise ValueError
        return path, data['after']
    except Exception:
        raise CommandError(CURSOR_FAILED) from None


def _save_cursor(path, state):
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.photo-prune-', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'after': state}, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class Command(BaseCommand):
    help = 'Dry-run a bounded scan of unreferenced private photos older than seven days.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', default=500, help='Maximum entries inspected across both private namespaces, at most 5000.')
        parser.add_argument('--min-age-hours', default=168, help='Minimum object age in hours, at least 168.')
        parser.add_argument('--delete', action='store_true', help='Explicitly remove eligible unreferenced objects; otherwise only count them.')
        parser.add_argument('--cursor-file', help='Private resumable cursor for object-backed storage. Its parent directory must be private and operator-owned.')

    def handle(self, *args, **options):
        limit, age = _number(options['limit']), _number(options['min_age_hours'])
        if not 1 <= limit <= 5000 or age < 168 or not isinstance(options['delete'], bool):
            raise CommandError(INVALID_OPTIONS)
        now = timezone.now()
        try:
            cutoff = now - timedelta(hours=age)
        except (OverflowError, ValueError):
            raise CommandError(INVALID_OPTIONS) from None
        cursor_path, state = _read_cursor(options['cursor_file'])

        scanned = deleted = 0
        candidates = []
        try:
            storage = storages['photos']
        except Exception:
            raise CommandError(FAILED) from None
        if cursor_path is not None and not getattr(storage, 'supports_private_photo_cursor', False):
            raise CommandError('Persistent photo cursors require object-backed storage.')
        try:
            iterator = getattr(storage, 'iter_private_photo_objects', None)
            if not callable(iterator):
                raise ValueError
            for index, prefix in enumerate(PRIVATE_PHOTO_PREFIXES):
                remaining = limit - scanned
                if remaining <= 0:
                    break
                # Reserve part of the total budget for each namespace, so a
                # busy conversation prefix does not prevent evidence checks.
                budget = max(1, remaining // (len(PRIVATE_PHOTO_PREFIXES) - index))
                page = iterator(prefix, budget, after=state.get(prefix)) if cursor_path is not None else iterator(prefix, budget)
                seen, last_key = 0, None
                try:
                    for key, modified in islice(page, budget):
                        seen += 1
                        scanned += 1
                        if _valid_marker(key, prefix):
                            last_key = key
                        if (not isinstance(key, str) or not key.startswith(prefix)
                                or not PRIVATE_KEY.fullmatch(key)
                                or not isinstance(modified, datetime) or timezone.is_naive(modified)
                                or modified > now or modified > cutoff):
                            continue
                        candidates.append(key)
                finally:
                    close = getattr(page, 'close', None)
                    if callable(close):
                        close()
                if cursor_path is not None:
                    state[prefix] = None if seen < budget else last_key or state.get(prefix)

            # Fetch references in two bounded queries before considering any
            # deletion, including retained copies for closed or resolved cases.
            referenced = set(PhotoAttachment.objects.filter(storage_key__in=candidates).values_list('storage_key', flat=True))
            referenced.update(PhotoEvidence.objects.filter(storage_key__in=candidates).values_list('storage_key', flat=True))
            eligible = list(dict.fromkeys(key for key in candidates if key not in referenced))
            if options['delete']:
                for key in eligible:
                    # Recheck immediately before deletion; the grace period
                    # also separates this task from in-flight random-key writes.
                    if (PhotoAttachment.objects.filter(storage_key=key).exists()
                            or PhotoEvidence.objects.filter(storage_key=key).exists()):
                        continue
                    storage.delete(key)
                    deleted += 1
            if cursor_path is not None:
                _save_cursor(cursor_path, state)
        except Exception:
            # Provider errors can include signed URLs, keys or credentials.
            # Never repeat them or print any object name or private filesystem.
            raise CommandError(FAILED) from None

        self.stdout.write(
            f'Private photo maintenance: scanned={scanned}, eligible={len(eligible)}, '
            f'deleted={deleted}, dry_run={not options["delete"]}.'
        )
