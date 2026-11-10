"""Explicit, fail-closed initialization of the bundled location reference data."""

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from locations.models import LGA, State
from locations.reference import NIGERIA_LOCATIONS


DATASET_MISMATCH_MESSAGE = (
    "Existing location data differs from the bundled reference data; "
    "no changes were made."
)


class Command(BaseCommand):
    help = (
        "Initialize 37 states/FCT and 774 LGAs in empty location tables. "
        "An exact existing dataset is preserved; other existing data is rejected."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            choices=tuple(connections),
            help="Database to initialize (default: default).",
        )

    def handle(self, *args, **options):
        database = options["database"]
        connection = connections[database]
        with transaction.atomic(using=database):
            if connection.vendor == "postgresql":
                # Serialize bootstrap attempts before reading either table. This
                # also blocks concurrent inserts while checking for an exact set.
                quote = connection.ops.quote_name
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"LOCK TABLE {quote(State._meta.db_table)}, "
                        f"{quote(LGA._meta.db_table)} IN SHARE ROW EXCLUSIVE MODE"
                    )

            states = list(State.objects.using(database).values_list("id", "name"))
            lgas = list(LGA.objects.using(database).values_list("state_id", "name"))

            if states or lgas:
                state_ids = {name: pk for pk, name in states}
                if set(state_ids) != set(NIGERIA_LOCATIONS):
                    raise CommandError(DATASET_MISMATCH_MESSAGE)
                expected_lgas = {
                    (state_ids[state], name)
                    for state, names in NIGERIA_LOCATIONS.items()
                    for name in names
                }
                if len(lgas) != len(expected_lgas) or set(lgas) != expected_lgas:
                    raise CommandError(DATASET_MISMATCH_MESSAGE)
                initialized = False
            else:
                State.objects.using(database).bulk_create(
                    [State(name=name) for name in NIGERIA_LOCATIONS],
                    batch_size=100,
                )
                # Read the generated keys explicitly for all supported backends.
                state_ids = dict(
                    State.objects.using(database).values_list("name", "id")
                )
                LGA.objects.using(database).bulk_create(
                    [
                        LGA(state_id=state_ids[state], name=name)
                        for state, names in NIGERIA_LOCATIONS.items()
                        for name in names
                    ],
                    batch_size=250,
                )
                initialized = True

        if initialized:
            self.stdout.write(self.style.SUCCESS("Initialized 37 states/FCT and 774 LGAs."))
        else:
            self.stdout.write("Bundled location data is already initialized; no changes made.")
