import csv
import os
from django.core.management.base import BaseCommand
from ppas.models import PPA
from locations.models import State, LGA

class Command(BaseCommand):
    help = 'Import nationwide PPAs from a CSV file or seed initial verified nationwide data'

    def add_arguments(self, parser):
        parser.add_argument('--file', type=str, help='Path to CSV file containing PPA data', required=False)

    def handle(self, *args, **options):
        file_path = options.get('file')
        if file_path and os.path.exists(file_path):
            self.import_from_csv(file_path)
        else:
            self.stdout.write(self.style.WARNING("No CSV file provided. Seeding verified nationwide sample records..."))
            self.seed_sample_nationwide_data()

    def import_from_csv(self, file_path):
        count = 0
        with open(file_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                state_name = row.get('state', '').strip()
                lga_name = row.get('lga', '').strip()
                name = row.get('name', '').strip()

                if not state_name or not lga_name or not name:
                    continue

                state_obj, _ = State.objects.get_or_create(name=state_name)
                lga_obj, _ = LGA.objects.get_or_create(name=lga_name, state=state_obj)

                lat = float(row['latitude']) if row.get('latitude') else None
                lon = float(row['longitude']) if row.get('longitude') else None
                verified = row.get('verified', 'true').lower() == 'true'

                PPA.objects.update_or_create(
                    name=name,
                    state=state_obj,
                    lga=lga_obj,
                    defaults={
                        'address': row.get('address', ''),
                        'category': row.get('category', 'Public Sector'),
                        'latitude': lat,
                        'longitude': lon,
                        'description': row.get('description', ''),
                        'phone': row.get('phone', ''),
                        'email': row.get('email', ''),
                        'google_maps_url': row.get('google_maps_url', ''),
                        'verified': verified,
                    }
                )
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} PPA records nationwide."))

    def seed_sample_nationwide_data(self):
        samples = [
            {"state": "Lagos", "lga": "Ikeja", "name": "Lagos State Secretariat Medical Center", "category": "Healthcare", "address": "Obafemi Awolowo Way, Ikeja, Lagos", "lat": 6.6018, "lon": 3.3515, "verified": True},
            {"state": "Lagos", "lga": "Lagos Island", "name": "First Bank of Nigeria PLC (Head Office)", "category": "Private Sector", "address": "35 Marina, Lagos Island, Lagos", "lat": 4.4538, "lon": 3.3946, "verified": True},
            {"state": "Rivers", "lga": "Port Harcourt", "name": "Rivers State University Teaching Hospital", "category": "Healthcare", "address": "Harcourt Rd, Port Harcourt, Rivers", "lat": 4.7774, "lon": 7.0134, "verified": True},
            {"state": "Kano", "lga": "Kano Municipal", "name": "Kano State Polytechnic", "category": "Education", "address": "BUK Road, Kano", "lat": 11.9964, "lon": 8.5167, "verified": True},
        ]
        for data in samples:
            state_obj, _ = State.objects.get_or_create(name=data["state"])
            lga_obj, _ = LGA.objects.get_or_create(name=data["lga"], state=state_obj)
            PPA.objects.get_or_create(
                name=data["name"],
                state=state_obj,
                lga=lga_obj,
                defaults={
                    'address': data["address"],
                    'category': data["category"],
                    'latitude': data["lat"],
                    'longitude': data["lon"],
                    'verified': data["verified"],
                }
            )
        self.stdout.write(self.style.SUCCESS("Nationwide verified sample records added successfully."))