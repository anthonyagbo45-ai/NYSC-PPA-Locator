# config/context_processors.py
import os

def google_maps_api(request):
    """
    Exposes the Google Maps API key to all Django templates.
    Access it in HTML using {{ GOOGLE_MAPS_API_KEY }}
    """
    return {
        'GOOGLE_MAPS_API_KEY': os.getenv('GOOGLE_MAPS_API_KEY', '')
    }