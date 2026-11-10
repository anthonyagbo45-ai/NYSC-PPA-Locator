import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from notifications.community_services import dispatch_community


class Command(BaseCommand):
    help = 'Deliver bounded, explicitly opted-in in-app community alerts. No email, SMS or push is sent.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=1000, help='Maximum in-app items per run (1–5000).')
        parser.add_argument('--dry-run', action='store_true', help='Count eligible deliveries without creating records.')

    def handle(self, *args, **options):
        try:
            result = dispatch_community(limit=options['limit'], dry_run=options['dry_run'])
        except ValidationError:
            raise CommandError('Choose a community alert limit between 1 and 5000.')
        self.stdout.write(json.dumps(result))
