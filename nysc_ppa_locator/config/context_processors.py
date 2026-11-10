from django.conf import settings


def google_maps_api(request):
    # Browser keys must have HTTP referrer restrictions; never expose the server key.
    return {'GOOGLE_MAPS_API_KEY': getattr(settings, 'GOOGLE_MAPS_BROWSER_API_KEY', '')}
