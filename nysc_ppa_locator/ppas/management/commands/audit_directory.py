"""Read-only counts for a provenance review; never emit directory or member data."""
import json
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.db.models.functions import Lower, Trim
from ppas.models import PPA


class Command(BaseCommand):
    help = ('Report directory provenance/status counts and possible duplicate name/location groups. '
            'Read-only: no names, addresses, coordinates or member information are printed.')

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', dest='as_json', help='Output a single JSON object.')

    def handle(self, *args, **options):
        counts = {'total_places': PPA.objects.count()}
        for field in ('source', 'status', 'location_status'):
            allowed_values = dict(PPA._meta.get_field(field).flatchoices)
            buckets = {}
            for row in PPA.objects.values(field).annotate(total=Count('pk')).order_by(field):
                key = row[field] if row[field] in allowed_values else 'unknown'
                buckets[key] = buckets.get(key, 0) + row['total']
            counts['by_' + field] = buckets
        candidates = (
            PPA.objects.annotate(normalized_name=Lower(Trim('name')))
            .values('normalized_name', 'state_id', 'lga_id')
            .annotate(total=Count('pk')).filter(total__gt=1)
        )
        counts['duplicate_name_location_groups'] = candidates.count()
        counts['duplicate_name_location_records'] = candidates.aggregate(duplicate_total=Sum('total'))['duplicate_total'] or 0
        if options['as_json']:
            self.stdout.write(json.dumps(counts, sort_keys=True))
        else:
            self.stdout.write('Directory audit (read-only counts; duplicate groups are candidates, not confirmed duplicates)')
            for key, value in counts.items():
                self.stdout.write(f'{key}: {json.dumps(value, sort_keys=True)}')
