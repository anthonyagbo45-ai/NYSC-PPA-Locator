import requests
from django.conf import settings
from django.db import models
from locations.models import State, LGA

class PPA(models.Model):
    CATEGORY_CHOICES = [
        ('Education', 'Education'),
        ('Public Sector', 'Public Sector'),
        ('Private Sector', 'Private Sector'),
        ('NGO', 'NGO'),
        ('Healthcare', 'Healthcare'),
    ]

    name = models.CharField(max_length=255, db_index=True)
    state = models.ForeignKey(State, on_delete=models.CASCADE, db_index=True)
    lga = models.ForeignKey(LGA, on_delete=models.CASCADE, db_index=True)
    address = models.TextField()
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Public Sector')
    
    # Geographic & Navigation fields
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    google_maps_url = models.URLField(max_length=500, blank=True, null=True)
    
    # Contact & Verification
    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    description = models.TextField(blank=True, null=True, default='')
    verified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'lga']),
            models.Index(fields=['name']),
        ]

    def save(self, *args, **kwargs):
        # Automatically fetch coordinates if missing and address/state/lga are available
        if (not self.latitude or not self.longitude) and self.address and self.state and self.lga:
            full_query = f"{self.name}, {self.address}, {self.lga.name}, {self.state.name}, Nigeria"
            api_key = settings.GOOGLE_MAPS_API_KEY
            
            if api_key:
                url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(full_query)}&key={api_key}"
                try:
                    response = requests.get(url)
                    data = response.json()
                    if data.get('status') == 'OK':
                        location = data['results'][0]['geometry']['location']
                        self.latitude = location['lat']
                        self.longitude = location['lng']
                except Exception as e:
                    print(f"Geocoding error: {e}")

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.lga.name}, {self.state.name})"


class Review(models.Model):
    ppa = models.ForeignKey(PPA, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey('accounts.CorpMember', on_delete=models.CASCADE)
    rating = models.PositiveIntegerField(default=5)
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Review for {self.ppa.name} by {self.user.state_code}"