from pathlib import Path
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def privacy(request):
    return render(request, 'privacy.html')


@require_GET
def terms(request):
    return render(request, 'terms.html')


@require_GET
def feedback(request):
    return render(request, 'feedback.html', {'support_email': getattr(settings, 'SUPPORT_EMAIL', '')})


@require_GET
def service_worker(request):
    content = (Path(settings.BASE_DIR) / 'static' / 'locator' / 'service-worker.js').read_text()
    response = HttpResponse(content, content_type='text/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


@require_GET
def health(request):
    """No version, database names, keys, or error details in public responses."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        cache.set('locator-health', 'ok', timeout=10)
        if cache.get('locator-health') != 'ok':
            raise RuntimeError('Cache unavailable')
    except Exception:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})
