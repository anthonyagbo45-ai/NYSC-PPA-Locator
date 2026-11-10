from locations.management.commands.initialize_locations import Command as InitializeLocationsCommand


class Command(InitializeLocationsCommand):
    help = 'Initialize the shared Nigeria reference data; existing noncanonical data requires review.'
