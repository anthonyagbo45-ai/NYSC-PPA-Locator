"""Security headers for static HTML which bypasses Django view middleware."""
def add_security_headers(headers, path, url):
    headers['X-Content-Type-Options'] = 'nosniff'
    headers['X-Frame-Options'] = 'DENY'
    headers['Referrer-Policy'] = 'same-origin'
    if str(url).split('?', 1)[0].endswith('.html'):
        headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
