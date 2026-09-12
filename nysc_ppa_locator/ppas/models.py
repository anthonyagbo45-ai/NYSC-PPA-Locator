from django.db import models
from django.conf import settings
from locations.models import State, LGA

class PPA(models.Model):
    CATEGORY_CHOICES = [
        ('Education', 'Education & Teaching'),
        ('Healthcare', 'Healthcare & Medicine'),
        ('Public Sector', 'Government & Public Sector'),
        ('Private Sector', 'Private Corporate & Tech'),
        ('NGO', 'NGOs & Non-Profits'),
        ('Legal', 'Legal & Law Firms'),
    ]

    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Education')
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name='ppas')
    lga = models.ForeignKey(LGA, on_delete=models.CASCADE, related_name='ppas')
    address = models.TextField()
    description = models.TextField(blank=True, null=True)
    
    # FIXED: Changed help_weight -> help_text
    stipend = models.PositiveIntegerField(blank=True, null=True, help_text="Monthly stipend in NGN")
    provides_accommodation = models.BooleanField(default=False)
    accepting_corpers = models.BooleanField(default=True)
    working_days = models.CharField(max_length=100, default="Mon - Fri")

    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)

    contact_email = models.EmailField(blank=True, null=True)
    contact_phone = models.CharField(max_length=20, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} - {self.lga.name}, {self.state.name}"


class Review(models.Model):
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ppa_reviews')
    rating = models.PositiveSmallIntegerField(choices=[(i, str(i)) for i in range(1, 6)])
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('ppa', 'user')

    def __str__(self):
        return f"{self.rating}★ by {self.user} for {self.ppa.name}"