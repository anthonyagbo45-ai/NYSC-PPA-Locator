from django.contrib import admin
from .models import PPA, Review

@admin.register(PPA)
class PPAAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'state', 'lga', 'verified', 'latitude', 'longitude')
    list_filter = ('verified', 'category', 'state', 'lga')
    search_fields = ('name', 'address', 'phone')
    list_editable = ('verified',)

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('ppa', 'user', 'rating', 'created_at')
    list_filter = ('rating', 'created_at')