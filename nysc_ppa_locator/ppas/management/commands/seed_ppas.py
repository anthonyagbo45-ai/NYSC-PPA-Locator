import random
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from locations.models import State, LGA
from ppas.models import PPA, Review

User = get_user_model()

# Sample data pools for realistic generation
PPA_PREFIXES = [
    "Community Secondary School", "General Hospital", "State Secretariat",
    "Ministry of Education", "High Court of Justice", "TechHub Innovation Lab",
    "Apex Comprehensive College", "St. Jude Specialist Clinic", "Global Ventures Ltd",
    "Federal Secretariat", "Local Government Secretariat", "Primary Health Center"
]

CATEGORIES = [
    "Education", "Healthcare", "Public Sector", "Private Sector", "NGO", "Legal"
]

SAMPLE_REVIEWS = [
    "Great work environment with supportive staff. Highly recommended for corps members!",
    "Stipend is paid promptly on the 25th of every month. Accommodation provided was decent.",
    "Heavy workload during weekdays, but weekends are completely free. Good learning curve.",
    "Very friendly staff, but accommodation is far from the location. Plan for transportation.",
    "Challenging environment but fantastic exposure if you want hands-on experience in this sector."
]

class Command(BaseCommand):
    help = "Seeds the database with mock States, LGAs, PPAs, and Reviews for testing."

    def add_arguments(self, parser):
        parser.add_argument(
            '--total',
            type=int,
            default=20,
            help='Indicates the number of PPA entries to create (default: 20)'
        )

    def handle(self, *args, **options):
        total = options['total']
        self.stdout.write(self.style.WARNING("Starting database seeding process..."))

        # 1. Ensure mock States & LGAs exist
        state, _ = State.objects.get_or_create(name="Federal Capital Territory")
        lgas = ["Abuja Municipal", "Gwagwalada", "Kuje", "Bwari"]
        lga_objects = []
        for lga_name in lgas:
            lga, _ = LGA.objects.get_or_create(name=lga_name, state=state)
            lga_objects.append(lga)

        # Also grab any existing States/LGAs if present
        all_states = list(State.objects.all())
        if not all_states:
            all_states = [state]

        # 2. Fetch or Create test User accounts for review authors
        test_user, _ = User.objects.get_or_create(
            email="tester@nysc.gov.ng",
            defaults={"full_name": "Test Corper", "state_code": "FC/24A/0001"}
        )
        if not test_user.has_usable_password():
            test_user.set_password("password123")
            test_user.save()

        # 3. Generate PPAs
        created_count = 0
        for i in range(total):
            chosen_state = random.choice(all_states)
            state_lgas = list(LGA.objects.filter(state=chosen_state))
            chosen_lga = random.choice(state_lgas) if state_lgas else random.choice(lga_objects)

            prefix = random.choice(PPA_PREFIXES)
            ppa_name = f"{prefix} ({chosen_lga.name})"

            # Approximate coordinates within Nigeria for Google Maps rendering
            lat = round(9.05785 + random.uniform(-0.5, 0.5), 6)
            lng = round(7.49508 + random.uniform(-0.5, 0.5), 6)

            ppa, created = PPA.objects.get_or_create(
                name=ppa_name,
                state=chosen_state,
                lga=chosen_lga,
                defaults={
                    "category": random.choice(CATEGORIES),
                    "address": f"No. {random.randint(1, 150)} Broad Street, {chosen_lga.name}",
                    "description": f"Official PPA facility operating in {chosen_lga.name}, offering positions for qualified NYSC corps members.",
                    "stipend": random.choice([15000, 20000, 25000, 30000, 50000, None]),
                    "provides_accommodation": random.choice([True, False]),
                    "accepting_corpers": random.choice([True, False]),
                    "latitude": lat,
                    "longitude": lng,
                    "contact_email": f"info@ppa{i+1}.org.ng",
                    "contact_phone": f"0803000{i:04d}",
                }
            )

            if created:
                created_count += 1

                # 4. Attach mock reviews to newly created PPAs
                Review.objects.create(
                    ppa=ppa,
                    user=test_user,
                    rating=random.randint(3, 5),
                    comment=random.choice(SAMPLE_REVIEWS)
                )

        self.stdout.write(
            self.style.SUCCESS(f"Successfully seeded {created_count} new PPA entries with mock reviews!")
        )