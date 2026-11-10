# PPA Locator

A free Django community platform for NYSC corps members: find a PPA, check community context, get directions and keep private service-year plans.

## Features

- Guest PPA search by state/LGA/name/category, app-owned details and directions.
- Reviewed independent PPA proposals, contextual contributions and reviews.
- Private assigned-PPA progress, bookmarks and history.
- Reviewed accommodation, transport estimates and local services with source and freshness labels.
- Private budgets and dated expenses.
- CDS/clearance resource workflows, private CDS preferences and opt-in in-app reminders.
- Corper Connect questions, answers, helpful reactions, resolution, private follows and contextual block/report tools.
- Moderated local rooms with explicit admission, public or approved-member reading and immediate leave/access revocation.
- Default-off in-app community alerts with current-access/version/block checks.
- Account recovery, email verification, OTP-protected administration and scoped moderation.

Publication approval is separate from independent verification. Community information and personal reminders do not grant NYSC posting, attendance or clearance approval. All ordinary member features are free.

## Development

Use Python 3.12+ and Node.js 22+ from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r nysc_ppa_locator/requirements.txt
npm ci --include=dev
npm run build:css
```

For a fresh development checkout, create local configuration only if none exists:

```bash
python - <<'PY'
import os, secrets
path = 'nysc_ppa_locator/.env'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as config:
    config.write('DEBUG=True\nDJANGO_SECRET_KEY=' + secrets.token_urlsafe(64) + '\nALLOWED_HOSTS=localhost,127.0.0.1,testserver\n')
PY
cd nysc_ppa_locator
python manage.py migrate --noinput
python manage.py bootstrap_roles
python manage.py runserver
```

An existing configuration is preserved; do not replace it with development values. Local SQLite is for development. Email defaults to in-memory delivery in development; interactive recovery/verification requires a securely configured mail service. Never print signed tokens to public logs. Provider access is optional for the saved community directory.

Run checks from the Django project directory:

```bash
python -m pip check
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test accounts locations ppas service_year budgeting local_guides service_resources community local_rooms notifications config
```

CI also exercises PostgreSQL 17 and Redis 7. Use disposable test services: tests clear their cache. Build styles with the locked dependencies rather than a runtime CDN. No React or separate frontend server is required.

## Production configuration

Configure values in the hosting provider's protected environment, using `nysc_ppa_locator/.env.example` as a variable-name reference. Keep credentials, personal databases, media and runtime environments out of source control.

| Configuration | Purpose |
| --- | --- |
| `DEBUG=False`, `DJANGO_SECRET_KEY` | Strong unique production secret, at least 50 characters |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Explicit application hostnames and trusted HTTPS origins |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | Persistent PostgreSQL connection; `DATABASE_URL` alone is not consumed |
| `POSTGRES_SSLMODE` | Defaults to `verify-full`; use the provider's trusted CA |
| `REDIS_URL` | Shared private Redis for throttles and provider budgets |
| `TRUST_PROXY_HTTPS` | Enable only behind a trusted edge that strips client forwarded headers |
| Mail host/port/user/password/TLS, `DEFAULT_FROM_EMAIL` | Real account verification and recovery delivery |
| `GOOGLE_MAPS_API_KEY` | Server-only restricted Places key |
| `GOOGLE_MAPS_BROWSER_API_KEY` | Separate referrer-restricted browser key for embedded maps |
| `GOOGLE_ROUTES_API_KEY` | Restricted server route-estimate key, if enabled |
| `SUPPORT_EMAIL` | Reachable support/privacy contact |

Set provider quotas/billing alerts, database connection budgets, protected off-host backups, restore exercises, monitoring and source/moderation responsibilities. Private events and finances are owner-scoped and absent from the moderation admin. Establish an appropriate retention/deletion process. Do not claim legal compliance solely from application controls.

## Existing-service deployment

Verify the existing service, repository root, linked branch, runtime, authoritative database and backup/recovery point before release. The build needs both Python and Node and access to the repository-root package files. Do not create an empty replacement database for an existing installation.

With the repository root as the verified deployment context:

- Build: `bash nysc_ppa_locator/build.sh`
- Reviewed one-time release/predeploy: `bash nysc_ppa_locator/release.sh`
- Start: `cd nysc_ppa_locator && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 30`
- Health check: `/health/`

Build installs locked frontend/Python dependencies and collects static assets without running migrations. Release applies additive migrations once, bootstraps role definitions without assigning users/staff/verifiers and runs deployment checks. WhiteNoise serves collected hashed/compressed assets. Do not run migrations independently in each worker. Confirm actual host/TLS/proxy/static settings and preserve existing records before enabling a new artifact.

Prefer a compatible prior application artifact for rollback while retaining additive schema and protected current configuration. Do not drop new history tables or restore a stale database over newer member writes.

## Fresh Render installation

Use this path only when deliberately starting with a new empty database. It keeps the upgraded application and imports no previous accounts, PPAs, reviews or recovery captures. Existing members will need to register again. Keep any older backup separately.

1. Create a persistent PostgreSQL database and a Redis-compatible Key Value service in the same region as the web service. Review their current prices, retention and expiry conditions. A provider's temporary/free database is not a permanent backup strategy. Member features remain free regardless of the operator's hosting plan.
2. Configure the existing Python web service for `main`, repository Root Directory blank, Node 22+ and Python 3.12+. Set `DEBUG=False`, a new strong `DJANGO_SECRET_KEY`, exact `ALLOWED_HOSTS=nysc-ppa-locator.onrender.com`, `CSRF_TRUSTED_ORIGINS=https://nysc-ppa-locator.onrender.com`, and `TRUST_PROXY_HTTPS=True` only for the trusted Render HTTPS ingress.
3. Enter the new database's individual `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` and the cache's `REDIS_URL` in Render's protected environment. `DATABASE_URL` alone is not consumed. PostgreSQL TLS defaults to `verify-full`; use the provider's supported hostname/CA rather than disabling verification to bypass an error.
4. For the first setup, set `PPA_INITIALIZE_LOCATIONS=True` and `PPA_BOOTSTRAP_ADMIN=True`. Enter `PPA_ADMIN_STATE_CODE`, `PPA_ADMIN_EMAIL`, `PPA_ADMIN_FULL_NAME` and a unique `PPA_ADMIN_PASSWORD` of at least 12 characters. These are operator-controlled deployment values, never public registration options or values to commit/share in chat.

With a supported pre-deploy hook, retain the separate build/release commands above. On a service without a pre-deploy hook or shell, the explicit fresh-install Build Command can run them sequentially:

```bash
bash nysc_ppa_locator/build.sh && bash nysc_ppa_locator/release.sh
```

The Start Command remains:

```bash
cd nysc_ppa_locator && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 30
```

Set the health-check path to `/health/`. The combined build/release command performs database changes before a new runtime starts; bind it only to the intended main service and database, not preview/test builds. Future releases require protected backups and backward-compatible migrations. Do not place migrations in each worker's startup path.

The explicit location command creates only 36 states plus FCT and 774 LGA/area-council reference choices. It does not seed demo PPAs or members. The bundled lists are application reference data, not an independently verified official registry. Existing exact reference rows retain their IDs on reruns; differing existing location data is rejected without alteration. Legacy `seed_locations` and `populate_states` commands now share this initializer.

The administrator command creates a single operator only when the user table is empty. A matching existing active staff superuser is preserved on reruns, including after members join. Existing members are never promoted, credentials/verification are never reset, and conflicting identities stop the command. PostgreSQL serializes first-setup operations. No schema migration or public administrator-registration endpoint is added.

After a successful deployment, sign in at `/login/` using the configured administrator **state code and app password**. Open `/account/mfa/setup/`, enroll an authenticator and privately save the recovery codes; then enter `/admin/`. Email remains unverified until the ordinary verification flow is completed. Confirm `/health/`, empty PPA/community directories and registration with the seeded locality choices. Remove the four `PPA_ADMIN_*` values and set both setup flags to `False` after initialization; subsequent releases retain the administrator and reference records.

Configure and test real email delivery before opening member contributions/recovery to users. If the hosting plan restricts SMTP, use a compatible HTTPS delivery backend or a hosting plan that supports the configured mail transport; an in-memory or console backend is not production delivery. Configure the separately restricted Google keys for discovery, embedded maps and optional route estimates. These external providers, actual Render networking/TLS and deployment remain hosting checks, not outcomes of local tests.

## Reminders and moderation

All global delivery choices start off; members also choose individual events, confirmed notices, followed questions or approved rooms. Community delivery starts with future activity after both choices are enabled; old replies are never backfilled. Only current eligible versions produce in-app reminders. Source/group changes, expiry or opt-out suppress stale alerts. This release sends no SMS, email reminder or browser push and does not schedule a hosting-provider job automatically.

```bash
python manage.py dispatch_community_notifications --dry-run --limit 1000
python manage.py dispatch_reminders --dry-run --limit 1000
python manage.py dispatch_reminders --limit 1000
python manage.py audit_freshness --json
```

Run a reviewed periodic reminder command only after confirming consent, deployment scheduling and operational limits. Members can explicitly check their own reminders in the app. Administrators enroll OTP through regular sign-in; superusers deliberately grant staff/roles. Independent verification permissions are separate from publication moderation. Inspect the exact supporting material for any real notice; an official homepage alone does not verify a local date.

Corper Connect is community advice, without official-answer badges or NYSC membership claims. Selecting a PPA or CDS reference never grants room admission. Public room text remains readable while signed out, including after an interaction block. Private one-to-one Messenger, photos, calls and careers remain later phases.
