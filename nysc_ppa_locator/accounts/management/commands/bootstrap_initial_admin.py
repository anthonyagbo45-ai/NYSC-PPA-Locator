import os

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction
from django.db.models import Q


CONFIG_ERROR = 'Initial administrator bootstrap configuration is invalid.'
VALIDATION_ERROR = 'Initial administrator bootstrap validation failed.'
CONFLICT_ERROR = 'Initial administrator bootstrap cannot use the existing accounts.'
DATABASE_ERROR = 'Initial administrator bootstrap could not complete.'
CREATED_MESSAGE = 'Initial administrator created.'
SKIPPED_MESSAGE = 'Initial administrator bootstrap skipped.'


class Command(BaseCommand):
    help = 'Create the first administrator from protected deployment environment variables.'

    def handle(self, *args, **options):
        enabled = os.environ.get('PPA_BOOTSTRAP_ADMIN', 'False').strip().lower()
        if enabled not in {'true', 'false'}:
            raise CommandError(CONFIG_ERROR)
        if enabled == 'false':
            self.stdout.write(SKIPPED_MESSAGE)
            return

        values = [os.environ.get(name, '') for name in (
            'PPA_ADMIN_STATE_CODE', 'PPA_ADMIN_EMAIL',
            'PPA_ADMIN_FULL_NAME', 'PPA_ADMIN_PASSWORD',
        )]
        if any(not value.strip() for value in values):
            raise CommandError(CONFIG_ERROR)
        state_code, email, full_name, password = values
        User = get_user_model()
        candidate = User(
            state_code=User.normalize_username(state_code.strip()),
            email=User.objects.normalize_email(email),
            full_name=full_name.strip(),
            is_active=True, is_staff=True, is_superuser=True,
            email_verified=False,
        )

        try:
            with transaction.atomic():
                # Row locks cannot serialize creation while the table is empty.
                # PostgreSQL holds this lock only until this transaction ends.
                if connection.vendor == 'postgresql':
                    table = connection.ops.quote_name(User._meta.db_table)
                    with connection.cursor() as cursor:
                        cursor.execute(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE')

                if User.objects.exists():
                    matches = list(User.objects.filter(
                        Q(state_code=candidate.state_code) | Q(email__iexact=candidate.email)
                    )[:2])
                    if not (len(matches) == 1
                            and matches[0].state_code == candidate.state_code
                            and User.objects.normalize_email(matches[0].email) == candidate.email
                            and matches[0].is_active and matches[0].is_staff
                            and matches[0].is_superuser):
                        raise CommandError(CONFLICT_ERROR)
                    created = False
                else:
                    if len(password) < 12:
                        raise ValidationError(VALIDATION_ERROR)
                    password_validation.validate_password(password, candidate)
                    candidate.set_unusable_password()
                    candidate.full_clean()
                    User.objects.create_superuser(
                        state_code=candidate.state_code, email=candidate.email,
                        full_name=candidate.full_name, password=password,
                        email_verified=False,
                    )
                    created = True
        except (ValidationError, ValueError):
            # Validators can include supplied credentials in their messages.
            raise CommandError(VALIDATION_ERROR) from None
        except DatabaseError:
            raise CommandError(DATABASE_ERROR) from None

        self.stdout.write(CREATED_MESSAGE if created else SKIPPED_MESSAGE)
