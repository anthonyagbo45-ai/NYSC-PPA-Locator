from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.db.models import Count
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from locations.management.commands.initialize_locations import DATASET_MISMATCH_MESSAGE
from locations.models import LGA, State
from locations.reference import EXPECTED_LGA_COUNTS, NIGERIA_LOCATIONS
from ppas.models import PPA


class LocationReferenceTests(SimpleTestCase):
    def test_complete_unique_reference_and_per_state_counts(self):
        expected_counts = {
            "Abia": 17, "Adamawa": 21, "Akwa Ibom": 31, "Anambra": 21,
            "Bauchi": 20, "Bayelsa": 8, "Benue": 23, "Borno": 27,
            "Cross River": 18, "Delta": 25, "Ebonyi": 13, "Edo": 18,
            "Ekiti": 16, "Enugu": 17, "Federal Capital Territory": 6,
            "Gombe": 11, "Imo": 27, "Jigawa": 27, "Kaduna": 23,
            "Kano": 44, "Katsina": 34, "Kebbi": 21, "Kogi": 21,
            "Kwara": 16, "Lagos": 20, "Nasarawa": 13, "Niger": 25,
            "Ogun": 20, "Ondo": 18, "Osun": 30, "Oyo": 33,
            "Plateau": 17, "Rivers": 23, "Sokoto": 23, "Taraba": 16,
            "Yobe": 17, "Zamfara": 14,
        }
        self.assertEqual(dict(EXPECTED_LGA_COUNTS), expected_counts)
        self.assertEqual(set(NIGERIA_LOCATIONS), set(expected_counts))
        self.assertEqual(len(NIGERIA_LOCATIONS), 37)
        self.assertEqual(sum(map(len, NIGERIA_LOCATIONS.values())), 774)
        pairs = set()
        for state, lgas in NIGERIA_LOCATIONS.items():
            with self.subTest(state=state):
                self.assertEqual(len(lgas), expected_counts[state])
                self.assertEqual(len(lgas), len(set(lgas)))
                self.assertTrue(all(name and name == name.strip() for name in lgas))
                pairs.update((state, name) for name in lgas)
        self.assertEqual(len(pairs), 774)
        self.assertNotIn("FCT", NIGERIA_LOCATIONS)

    def test_missing_lgas_and_spelling_corrections(self):
        required = {
            "Bauchi": {"Gamawa", "Tafawa Balewa"},
            "Ebonyi": {"Izzi"}, "Ekiti": {"Ikere"}, "Imo": {"Onuimo"},
            "Nasarawa": {"Doma"}, "Osun": {"Ayedaade", "Ayedire"},
            "Oyo": {"Oluyole"}, "Rivers": {"Akuku-Toru", "Emohua"},
            "Sokoto": {"Tangaza"}, "Zamfara": {"Tsafe"},
            "Gombe": {"Yamaltu/Deba"}, "Taraba": {"Kurmi", "Lau"},
            "Enugu": {"Nsukka"}, "Niger": {"Gurara"},
        }
        for state, names in required.items():
            with self.subTest(state=state):
                self.assertTrue(names.issubset(NIGERIA_LOCATIONS[state]))
        for state, obsolete in {
            "Taraba": "Kumi", "Gombe": "Yamaltu/Debba", "Imo": "Unuimo",
            "Rivers": "Emuoha", "Jigawa": "Kufe",
        }.items():
            with self.subTest(state=state):
                self.assertNotIn(obsolete, NIGERIA_LOCATIONS[state])


class InitializeLocationsTests(TestCase):
    def test_legacy_command_names_are_discoverable_and_preserve_the_same_reference_rows(self):
        output = StringIO()
        call_command('seed_locations', stdout=output)
        before = self.snapshot()
        call_command('populate_states', stdout=output)
        self.assertEqual(self.snapshot(), before)
        call_command('initialize_locations', stdout=output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((State.objects.count(), LGA.objects.count()), (37, 774))
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(PPA.objects.exists())

    def initialize(self):
        output = StringIO()
        call_command("initialize_locations", stdout=output)
        return output.getvalue()

    def snapshot(self):
        return (
            list(State.objects.order_by("pk").values_list("pk", "name")),
            list(LGA.objects.order_by("pk").values_list("pk", "state_id", "name")),
        )

    def assert_rejected_without_changes(self):
        before = self.snapshot()
        with self.assertRaises(CommandError) as error:
            self.initialize()
        self.assertEqual(str(error.exception), DATASET_MISMATCH_MESSAGE)
        self.assertEqual(self.snapshot(), before)

    def test_initialize_empty_tables_with_bounded_queries_and_correct_relations(self):
        with CaptureQueriesContext(connection) as captured:
            output = self.initialize()
        self.assertLessEqual(len(captured), 12)
        self.assertIn("Initialized 37 states/FCT and 774 LGAs.", output)
        self.assertEqual(State.objects.count(), 37)
        self.assertEqual(LGA.objects.count(), 774)
        self.assertEqual(
            dict(State.objects.annotate(total=Count("lgas")).values_list("name", "total")),
            dict(EXPECTED_LGA_COUNTS),
        )
        self.assertEqual(
            set(LGA.objects.values_list("state__name", "name")),
            {(state, name) for state, names in NIGERIA_LOCATIONS.items() for name in names},
        )
        self.assertEqual(PPA.objects.count(), 0)
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_exact_existing_dataset_is_idempotent_and_preserves_primary_keys(self):
        self.initialize()
        before = self.snapshot()
        output = self.initialize()
        self.assertIn("already initialized; no changes made", output)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_existing_records_and_related_data_are_preserved(self):
        state = State.objects.create(name="Existing state")
        lga = LGA.objects.create(name="Existing council", state=state)
        member = get_user_model().objects.create_user(
            state_code="TEST/26A/1", full_name="Test member",
            state_posted=state, lga_posted=lga,
        )
        ppa = PPA.objects.create(
            name="Existing organisation", state=state, lga=lga,
            address="Existing address", submitted_by=member,
        )
        self.assert_rejected_without_changes()
        member.refresh_from_db()
        ppa.refresh_from_db()
        self.assertEqual((member.state_posted_id, member.lga_posted_id), (state.pk, lga.pk))
        self.assertEqual((ppa.state_id, ppa.lga_id, ppa.submitted_by_id), (state.pk, lga.pk, member.pk))
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(PPA.objects.count(), 1)

    def test_single_canonical_state_is_not_completed_automatically(self):
        State.objects.create(name="Lagos")
        self.assert_rejected_without_changes()

    def test_missing_lga_in_existing_dataset_is_rejected(self):
        self.initialize()
        LGA.objects.filter(state__name="Taraba", name="Lau").delete()
        self.assert_rejected_without_changes()

    def test_same_counts_with_wrong_state_lga_assignment_are_rejected(self):
        self.initialize()
        lga = LGA.objects.get(state__name="Taraba", name="Lau")
        lga.state = State.objects.get(name="Lagos")
        lga.save(update_fields=["state"])
        self.assert_rejected_without_changes()

    def test_renamed_canonical_state_is_rejected_without_renaming(self):
        self.initialize()
        State.objects.filter(name="Federal Capital Territory").update(name="FCT")
        self.assert_rejected_without_changes()

    def test_partial_insert_failure_rolls_back_states_and_lgas(self):
        original_bulk_create = QuerySet.bulk_create

        def insert_then_fail(queryset, objects, **kwargs):
            if queryset.model is LGA:
                original_bulk_create(queryset, objects[:1], **kwargs)
                raise RuntimeError("Simulated LGA insertion failure")
            return original_bulk_create(queryset, objects, **kwargs)

        with patch.object(QuerySet, "bulk_create", autospec=True, side_effect=insert_then_fail):
            with self.assertRaisesMessage(RuntimeError, "Simulated LGA insertion failure"):
                self.initialize()
        self.assertEqual(State.objects.count(), 0)
        self.assertEqual(LGA.objects.count(), 0)
        self.assertEqual(PPA.objects.count(), 0)
        self.assertEqual(get_user_model().objects.count(), 0)


@skipUnless(connection.vendor == "postgresql", "Concurrent bootstrap uses PostgreSQL table locks.")
class ConcurrentLocationInitializationTests(TransactionTestCase):
    def test_concurrent_initializers_create_once_and_preserve_the_dataset(self):
        ready = Barrier(2)

        def initialize():
            try:
                ready.wait(timeout=10)
                output = StringIO()
                call_command("initialize_locations", stdout=output)
                return output.getvalue()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize) for _ in range(2)]
            outputs = [future.result(timeout=30) for future in futures]

        self.assertEqual(sum(output.startswith("Initialized ") for output in outputs), 1)
        self.assertEqual(sum("already initialized" in output for output in outputs), 1)
        self.assertEqual(State.objects.count(), 37)
        self.assertEqual(LGA.objects.count(), 774)
        self.assertEqual(
            set(LGA.objects.values_list("state__name", "name")),
            {(state, name) for state, names in NIGERIA_LOCATIONS.items() for name in names},
        )
