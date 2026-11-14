# Supabase PostgreSQL and Storage rollout

This guide covers a staged migration. The application defaults to SQLite and
local uploads. Do not change production settings or run an import until the
source database, staging results, and rollback plan have been approved.

## 1. Prepare Supabase

1. Create a disposable Supabase staging project. Do not use the production
   project for tests.
2. Confirm the staging database is empty, then apply
   [`../postgres_schema.sql`](../postgres_schema.sql) in the Supabase SQL
   Editor as the project database owner. The script creates the current
   application schema, uses `TEXT` timestamps and `INTEGER` flags, enables
   RLS, and revokes `anon`/`authenticated` access to the application tables.
   There are no client-facing RLS policies; the Flask server uses the
   privileged database role.
3. Use the **Session Pooler** connection URL on port **5432** for
   `DATABASE_URL`. The app enforces TLS with `sslmode=require`. Keep the URL
   server-side; never add it to source control, templates, browser JavaScript,
   or logs.
4. Create three **private** Storage buckets:
   - `barangayconnect-requests` (JPG, JPEG, PNG, WEBP, PDF; 5 MiB per object)
   - `barangayconnect-complaints` (JPG, JPEG, PNG, WEBP, PDF, MP4, MOV, WEBM,
     AVI, M4V; 5 MiB per object)
   - `barangayconnect-profiles` (JPG, PNG; 20 MiB per object)

   The application verifies each bucket is private before access. It does not
   create buckets or change their access settings automatically.

## 2. Identify and dry-run the data source

The local workspace currently contains multiple SQLite candidates. A read-only
inventory found 163 total rows in `instance/barangayconnect.db`, 162 in
`instance/backups/barangayconnect-2026-11-10_191212.db`, 22 in
`instance/barangayconnect_backup.db`, 22 in the November 9 backup, and 0 in the
November 7 backup. The active database and latest backup differ by one row.
These counts do not establish which file should be authoritative. Compare the
source with the owner's expected latest data before choosing it.

The importer requires an explicit source path, opens it read-only, checks the
supported tables and source foreign keys, and reports table counts/checksums.
It never selects or modifies a source automatically:

```powershell
.\venv\Scripts\python.exe .\migrate_sqlite_to_postgres.py --source .\instance\barangayconnect.db
```

Default mode is dry-run and never connects to a PostgreSQL database. The source
schema must match the importer's supported schema; older backups that do not
match are rejected rather than silently losing columns or records.

## 3. Import into disposable staging

After applying the schema to an empty staging project, provide its pooler URL
only in `STAGING_DATABASE_URL`, then run:

```powershell
.\venv\Scripts\python.exe .\migrate_sqlite_to_postgres.py --source .\instance\barangayconnect.db --confirm-import --report .\staging-database-migration-report.json
```

The staging importer refuses any existing application records or schema
mismatch. It uses one transaction, preserves IDs/password hashes and TEXT
values, synchronizes identity sequences, checks foreign keys, and compares
per-table row counts/checksums before commit. When a report is requested, its
path is reserved before connecting; verification is recorded before the
transaction commits. A failed import rolls back. The report contains counts
and digests, not row contents or credentials.

Only after explicit production-import approval, a verified source backup, and a
successful staging rehearsal may the same utility use `DATABASE_URL` with
`--confirm-production-import`. It still refuses to write to any destination
table that already contains records. No production import has been performed
as part of this change.

## 4. Transfer existing uploads

First run a read-only scan using the *same chosen SQLite source*:

```powershell
.\venv\Scripts\python.exe .\migrate_uploads_to_supabase.py --source-db .\instance\barangayconnect.db --upload-root .\uploads
```

The scan inventories only database-referenced files, confirms local file
presence, and reports total bytes. It does not upload anything. For staging,
set `STAGING_SUPABASE_URL`, `STAGING_SUPABASE_SERVICE_ROLE_KEY`, and, if needed,
the `STAGING_SUPABASE_STORAGE_BUCKET_*` names, then run:

```powershell
.\venv\Scripts\python.exe .\migrate_uploads_to_supabase.py --source-db .\instance\barangayconnect.db --upload-root .\uploads --confirm-upload --report .\staging-storage-migration-report.json
```

Each uploaded object is downloaded again and SHA-256 verified. The utility
refuses missing source files and uses non-upsert uploads so it cannot overwrite
an existing object. A partial failure records how many files were verified.
Retry with a new report path: already-present objects are downloaded and
checksum-verified instead of uploaded again; objects with different contents
are refused and never overwritten. The report path is reserved before transfer,
so an existing report prevents any upload.

Only after explicit production-transfer approval may the production
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` be used with
`--confirm-production-upload`. Keep the service-role key exclusively in the
server environment. The app streams files through existing authenticated,
role-checked routes; it does not issue public or long-lived file URLs.

Keep `STORAGE_BACKEND=local` until the staging transfer has passed its full
file-count and checksum review. Then set `STORAGE_BACKEND=supabase` only in
staging and verify request attachments, complaint evidence, and profile photos.

## 5. Staging test and production cutover

Before cutover, run the full SQLite suite and exercise the app against the
disposable PostgreSQL staging project. Verify resident login, password reset,
role/office restrictions, request/complaint creation and updates, notifications,
analytics filters, reports/exports/printing, and private upload access. Confirm
unauthorized users cannot access another resident's files and that the browser
cannot access bucket objects directly.

The opt-in PostgreSQL integration tests exercise adapter rollback and row access,
login, office permissions, authenticated upload access, and analytics/report
routes. They accept the standard Supabase Session Pooler URL (whose database
name is usually `postgres`) and require explicit disposable-database
confirmation, with `postgres_schema.sql` already applied. Test records are
removed after each run; the tests never initialize or reset the database:

```powershell
$secret = Read-Host "Staging Session Pooler URL" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:TEST_DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    $env:TEST_DATABASE_IS_DISPOSABLE = "YES"
    $env:ENABLE_POSTGRES_TESTS = "YES"
.\venv\Scripts\python.exe -m pytest -q tests/test_postgres_integration.py
}
finally {
    Remove-Item Env:\TEST_DATABASE_URL, Env:\TEST_DATABASE_IS_DISPOSABLE, Env:\ENABLE_POSTGRES_TESTS -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $secret.Dispose()
}
```

These tests use local temporary files for route authorization. Validate actual
Supabase Storage access separately in staging before switching
`STORAGE_BACKEND=supabase`. Storage failures are reported without saving
database file references; previously uploaded files in a failed multi-file
request are cleaned up when possible, and cleanup failures are logged.

## Local read-only staging verification

`verify_supabase_staging_readonly.py` uses only `STAGING_DATABASE_URL`,
`STAGING_SUPABASE_URL`, and `STAGING_SUPABASE_SERVICE_ROLE_KEY`. It forces a
read-only PostgreSQL transaction, reports only the application table/row
totals, then downloads each database-referenced object through the private
Storage API in memory and compares the downloaded byte length with Storage's
metadata. It does not write database records or files, and it refuses staging
URLs that match the corresponding production variables.

Set staging values only in the current PowerShell process. For secrets, use
masked prompts; do not put real values in `.env`, source files, command
arguments, or chat:

```powershell
function Set-SecretEnvironmentVariable($name) {
    $secure = Read-Host $name -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Environment]::SetEnvironmentVariable(
            $name,
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer),
            "Process"
        )
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
}

Set-SecretEnvironmentVariable "STAGING_DATABASE_URL"
$env:STAGING_SUPABASE_URL = Read-Host "Staging project URL"
Set-SecretEnvironmentVariable "STAGING_SUPABASE_SERVICE_ROLE_KEY"
.\venv\Scripts\python.exe .\verify_supabase_staging_readonly.py
```

The staging database URL must be a TLS Session Pooler URL (port 5432). The
service-role key is used only server-side for private bucket metadata and object
GET requests. Set `STAGING_SUPABASE_STORAGE_BUCKET_REQUESTS`,
`STAGING_SUPABASE_STORAGE_BUCKET_COMPLAINTS`, and
`STAGING_SUPABASE_STORAGE_BUCKET_PROFILES` in the same process only if the
staging bucket names differ from the documented defaults.

After the read-only verification succeeds, the app can be launched locally
against staging in that PowerShell process:

```powershell
$env:APP_ENV = "development"
$env:DB_BACKEND = "postgres"
$env:DATABASE_URL = $env:STAGING_DATABASE_URL
$env:STORAGE_BACKEND = "supabase"
$env:SUPABASE_URL = $env:STAGING_SUPABASE_URL
$env:SUPABASE_SERVICE_ROLE_KEY = $env:STAGING_SUPABASE_SERVICE_ROLE_KEY
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$randomBytes = New-Object byte[] 48
$rng.GetBytes($randomBytes)
$env:SECRET_KEY = [Convert]::ToBase64String($randomBytes)
$rng.Dispose()
$env:FLASK_DEBUG = "0"
.\venv\Scripts\python.exe .\app.py
```

Flask binds to `127.0.0.1:5000`; do not expose this service publicly. The app
supports writes, so avoid submitting requests/complaints or uploading files
until you explicitly intend to add staging records/objects. Use a dedicated
staging account for authentication checks; failed login attempts can update
login-lockout state. Clear the staging credentials and secrets when finished:

```powershell
Remove-Item Env:\STAGING_DATABASE_URL, Env:\STAGING_SUPABASE_URL, Env:\STAGING_SUPABASE_SERVICE_ROLE_KEY, Env:\STAGING_SUPABASE_STORAGE_BUCKET_REQUESTS, Env:\STAGING_SUPABASE_STORAGE_BUCKET_COMPLAINTS, Env:\STAGING_SUPABASE_STORAGE_BUCKET_PROFILES, Env:\DATABASE_URL, Env:\SUPABASE_URL, Env:\SUPABASE_SERVICE_ROLE_KEY, Env:\SUPABASE_STORAGE_BUCKET_REQUESTS, Env:\SUPABASE_STORAGE_BUCKET_COMPLAINTS, Env:\SUPABASE_STORAGE_BUCKET_PROFILES, Env:\SECRET_KEY, Env:\APP_ENV, Env:\DB_BACKEND, Env:\STORAGE_BACKEND, Env:\FLASK_DEBUG -ErrorAction SilentlyContinue
```

For Render, the included `render.yaml` uses only:

```text
Build: pip install -r requirements.txt
Start: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

There is deliberately no pre-deploy initialization, seed, or admin setup
command. Set these Render variables in the service dashboard:

```text
APP_ENV
DB_BACKEND
DATABASE_URL
STORAGE_BACKEND
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
SUPABASE_STORAGE_BUCKET_REQUESTS
SUPABASE_STORAGE_BUCKET_COMPLAINTS
SUPABASE_STORAGE_BUCKET_PROFILES
SECRET_KEY
```

Set `APP_ENV=production`, `DB_BACKEND=postgres`, and
`STORAGE_BACKEND=supabase`. Use only the Session Pooler URL at port 5432 for
`DATABASE_URL`. Configure `SECRET_KEY` as a strong generated value. Configure
the SMTP/Brevo variables separately if email delivery is required. Do not put
real values in `render.yaml`.

Cutover checklist:

1. Freeze writes to the old app and make a verified SQLite backup without
   changing or deleting the source database or its backups.
2. Re-run the source and upload dry-runs and compare the selected source to the
   expected current data. The active SQLite database is not assumed authoritative
   solely because it has the most rows.
3. Apply the PostgreSQL schema to the empty production project, then perform
   the separately approved production import and upload transfer.
4. Review both reports, table counts, password-hash-inclusive checksums, foreign
   keys, and file hashes before changing Render settings.
5. Set the Render variables above, deploy only after approval, and run smoke
   tests for login, read/write workflows, office permissions, reports, and
   private file access.
6. Restart/redeploy once and verify database records and uploads remain
   available. Confirm that no production request can initialize an empty
   database automatically.

## Backups, recovery, and rollback

- Supabase-managed backups and point-in-time recovery depend on the selected
  Supabase plan. Confirm the plan's retention and restore procedure before
  cutover, and test recovery against a disposable project. See the current
  [Supabase backup guide](https://supabase.com/docs/guides/platform/backups).
- Admin → Settings can create a manual PostgreSQL custom-format `pg_dump`
  download when `pg_dump` is installed (or `PG_DUMP_PATH` is configured).
  Render's local filesystem is ephemeral; download backups to a protected,
  separate location. PostgreSQL automatic local backups are disabled.
- Keep the original SQLite file, its backups, and the original local uploads
  unchanged until reconciliation and the rollback window are complete.
- For rollback, put the app in maintenance/read-only mode, retain the
  PostgreSQL database and all post-cutover writes, and restore the prior code
  and settings only after checking whether any records were written after
  cutover. Switching back to an old SQLite snapshot alone would omit new
  PostgreSQL records. Reconcile or export those writes before making SQLite
  authoritative.
- If PostgreSQL must be restored, use the Supabase plan's documented recovery
  process or a verified `pg_dump` backup against a separate restore target
  first. Do not restore over a live database without an approved recovery plan.
- This Render configuration has no persistent disk. Render documents that
  service-local files are ephemeral, so generated dump files are temporary;
  see [Render persistent disks](https://render.com/docs/disks). The application
  does not provide an automated restore endpoint. Use Supabase's dashboard or
  documented CLI/operator recovery after validation, reauthentication, a
  maintenance window, and a fresh pre-restore snapshot.
- Never run `seed.py`, delete/recreate a database, or use production as a test
  target during recovery.
