from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower

class CorpMemberManager(BaseUserManager):
    """Custom user manager where state_code is the unique identifier for authentication."""

    @classmethod
    def normalize_email(cls, email):
        # Preserve plus addressing: tags are part of an address, not aliases we
        # can safely discard. Only canonicalize casing/outer whitespace.
        return email.strip().lower() if email else None
    
    def create_user(self, state_code, email=None, password=None, **extra_fields):
        if not state_code:
            raise ValueError("The State Code must be set")
        email = self.normalize_email(email)
        user = self.model(state_code=state_code, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, state_code, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get('is_superuser') is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(state_code, email, password, **extra_fields)


class CorpMember(AbstractUser):
    username = None  # Remove default username field
    email = models.EmailField(unique=True, blank=True, null=True)
    full_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=80, blank=True, help_text='Optional public name shown on reviews. Your full name and state code remain private.')
    email_verified = models.BooleanField(default=False)
    state_code = models.CharField(max_length=20, unique=True, help_text="e.g., LA/25A/1234")
    
    # Relationships to locations app
    state_posted = models.ForeignKey('locations.State', on_delete=models.SET_NULL, null=True, blank=True)
    lga_posted = models.ForeignKey('locations.LGA', on_delete=models.SET_NULL, null=True, blank=True)
    
    # Use state_code as the primary login identifier
    USERNAME_FIELD = 'state_code'
    REQUIRED_FIELDS = ['full_name', 'email']

    # Attach the custom manager
    objects = CorpMemberManager()

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                Lower('email'),
                condition=models.Q(email__isnull=False) & ~models.Q(email=''),
                name='accounts_email_case_insensitive_unique',
            ),
        ]

    def clean(self):
        # AbstractUser.clean normalizes every email, including historical
        # addresses on unrelated ModelForm edits. Normalize new/changed email
        # writes in save instead, while retaining the base identifier cleaning.
        original_email = self.email
        super().clean()
        self.email = original_email

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        writes_email = update_fields is None or 'email' in update_fields
        if writes_email and 'email' in self.__dict__:
            previous_email = None
            if not self._state.adding:
                previous_email = self.__class__.objects.using(kwargs.get('using') or self._state.db).filter(pk=self.pk).values_list('email', flat=True).first()
            if self._state.adding or self.email != previous_email:
                self.email = self.__class__.objects.normalize_email(self.email)
                if not self._state.adding:
                    self.email_verified = False
                    if update_fields is not None:
                        kwargs['update_fields'] = set(update_fields) | {'email_verified'}
        super().save(*args, **kwargs)

    @property
    def public_name(self):
        return self.display_name.strip() or 'Corps member'

    def __str__(self):
        return f"Member #{self.pk or 'new'}"


class ContributorVerification(models.Model):
    """Private, append-only operator evidence; independent of account roles.

    A record describes an operator's contributor review, never NYSC identity,
    official status, or confirmation that an organisation accepts corps members.
    """

    class Outcome(models.TextChoices):
        CONFIRMED = 'confirmed', 'Contributor evidence checked'
        UNCONFIRMED = 'unconfirmed', 'Evidence insufficient'
        REVOKED = 'revoked', 'Earlier contributor check withdrawn'

    member = models.ForeignKey(CorpMember, on_delete=models.CASCADE, related_name='contributor_checks')
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    evidence_summary = models.TextField(max_length=2000, help_text='Private operator notes. Record only necessary evidence, no identity documents, passwords or sensitive personal details.')
    reviewed_by = models.ForeignKey(CorpMember, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_contributor_checks')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        default_permissions = ('add', 'view')
        indexes = [models.Index(fields=['member', '-created_at'], name='account_contributor_review_idx')]

    def __str__(self):
        return f'Contributor check #{self.pk or "new"} — {self.get_outcome_display()}'
