from django.contrib.auth.models import AbstractUser
from django.db import models

class CorpMember(AbstractUser):
    # Use email as the primary identifier
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    state_code = models.CharField(max_length=20, unique=True, help_text="e.g., LA/25A/1234")
    
    # Relationships to locations app
    state_posted = models.ForeignKey('locations.State', on_delete=models.SET_NULL, null=True)
    lga_posted = models.ForeignKey('locations.LGA', on_delete=models.SET_NULL, null=True)
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name', 'state_code']

    def __str__(self):
        return self.full_name