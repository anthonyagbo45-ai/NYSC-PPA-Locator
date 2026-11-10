"""Counts only: useful for a scheduled operator check without exposing private content."""
import json
from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone
from local_guides.models import Listing


class Command(BaseCommand):
    help = 'Read-only local guidance freshness counts; sends no reminders and exposes no member information.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Output machine-readable counts only.')

    def handle(self, *args, **options):
        rows = Listing.objects.values('kind', 'publication_status').annotate(
            total=Count('pk'), stale=Count('pk', filter=Q(expires_at__lte=timezone.now())),
        ).order_by('kind', 'publication_status')
        report = {'groups': list(rows), 'read_only': True, 'reminders_sent': 0}
        if options['json']:
            self.stdout.write(json.dumps(report))
        else:
            for row in report['groups']:
                self.stdout.write(f"{row['kind']} / {row['publication_status']}: {row['total']} entries; {row['stale']} due for reconfirmation")
            self.stdout.write('Read-only audit. No records changed or notifications sent.')
