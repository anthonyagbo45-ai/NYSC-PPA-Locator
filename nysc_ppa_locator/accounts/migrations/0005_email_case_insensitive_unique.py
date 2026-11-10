from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower


def check_existing_email_collisions(apps, schema_editor):
    Member = apps.get_model('accounts', 'CorpMember')
    collisions = (
        Member.objects.using(schema_editor.connection.alias)
        .exclude(email__isnull=True).exclude(email='')
        .annotate(canonical_email=Lower('email')).values('canonical_email')
        .annotate(account_count=Count('pk')).filter(account_count__gt=1)
    )
    if collisions.exists():
        # Fail before adding the constraint. Never merge users, rewrite old
        # addresses, or expose account details in deployment logs.
        raise RuntimeError('Case-insensitive account email collisions exist. Resolve ownership privately with the account holders before retrying this migration; no accounts were merged or changed.')


class Migration(migrations.Migration):
    dependencies = [('accounts', '0004_corpmember_display_name_corpmember_email_verified')]
    operations = [
        migrations.RunPython(check_existing_email_collisions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='corpmember',
            constraint=models.UniqueConstraint(
                Lower('email'), condition=models.Q(email__isnull=False) & ~models.Q(email=''),
                name='accounts_email_case_insensitive_unique',
            ),
        ),
    ]
