from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

class CorpMemberManager(BaseUserManager):
    """Custom user manager where state_code is the unique identifier for authentication."""
    
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
    state_code = models.CharField(max_length=20, unique=True, help_text="e.g., LA/25A/1234")
    
    # Relationships to locations app
    state_posted = models.ForeignKey('locations.State', on_delete=models.SET_NULL, null=True, blank=True)
    lga_posted = models.ForeignKey('locations.LGA', on_delete=models.SET_NULL, null=True, blank=True)
    
    # Use state_code as the primary login identifier
    USERNAME_FIELD = 'state_code'
    REQUIRED_FIELDS = ['full_name', 'email']

    # Attach the custom manager
    objects = CorpMemberManager()

    def __str__(self):
        return f"{self.full_name} ({self.state_code})"