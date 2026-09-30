# AWS identity storage

The AWS build uses PostgreSQL through `PostgresUIDStore`. The local build continues to use SQLite. PostgreSQL stores namespaces, inbox identities, immutable UID assignments, active membership, recent state, UIDVALIDITY, UIDNEXT and revisions. Inactive assignments remain as history: restoring a removed message allocates a higher UID. Mail bodies and raw credentials are never persisted here.

The PostgreSQL schema version is **2**; startup and readiness reject older versions. SQLite schema version 1 is translated explicitly to PostgreSQL version 2 during import; its generation and identity values are preserved. No automatic in-place database upgrade or reset is attempted.

Each EC2 container startup runs `scripts/ensure_database.py` before serving. A PostgreSQL advisory lock serializes initialization: an empty database is initialized once, while existing schemas and counters are validated without silent resets. The approved deployment starts with fresh hosted UID mappings. Database settings come from a private encrypted S3 runtime environment object populated from Pulumi encrypted configuration, with `sslmode=verify-full` and the bundled RDS root certificate. The application database user needs table operations, sequence access and lease-table operations. Production backup is an RDS snapshot or `pg_dump`, not the SQLite backup command.

A row lock protects each allocation transaction. A renewable mailbox lease serializes the complete authorized scan across containers. Acquisition, renewal and release use short SQL transactions; API work holds no database connection. Leases expire after 30 seconds and renew every 10 seconds. Each takeover increases a fencing token checked under a row lock inside UID reconciliation and snapshot writes. A stale or expired owner cannot commit, and failed renewal cancels its scan while retaining the prior client view. Revision checks additionally reject scans that overlap read-state mutation. Read-state updates invalidate snapshots and advance revisions both before and after the upstream request, with the lease token verified in each transaction. A crashed worker may leave an ambiguously completed remote API mutation because API and PostgreSQL changes cannot be committed atomically; the next full scan reconciles that upstream result. Cancellation during lease acquisition can leave a lease until its bounded 30-second expiry. Snapshot coalescing caches only IDs, flags, timestamps and sizes by credential SHA256; the server supplies a freshness cutoff measured using the database clock, preventing worker clock skew and invalidates snapshots after flag writes. Old snapshots are purged on subsequent writes after five minutes. These are coordination metadata, not an authorization cache.

A shared request budget is keyed by SHA256 credential fingerprint. Requests reserve an immediate start atomically or wait and retry; no future slots are queued. Shared cooldowns honor throttling across containers. Signed downloads check cooldown without consuming API starts. This coordinates dispatch frequency; download concurrency remains per process, so two workers can each perform their configured concurrent downloads.

## Preserve local UID identities during cutover

1. Stop the local IMAP process and the AWS service. Prevent overlapping writes during the cutover.
2. Create a consistent SQLite backup using the local backup command. Transfer that private file to the operator host/container; it contains message IDs but no API keys.
3. Initialize the empty PostgreSQL database explicitly.
4. In the AWS project, export the database connection secret as `IMAP_DATABASE_URL` and run `python -m scripts.migrate_uid_store --sqlite /path/to/backup.sqlite3`.
5. The importer runs one PostgreSQL transaction and refuses a nonempty destination. It translates the schema-version setting and preserves the generation setting, mailbox ID, UIDVALIDITY, UIDNEXT, revision, active/inactive assignment and recent flag. It advances the identity sequence and verifies imported row count before committing.
6. Start AWS containers, verify LOGIN, SELECT and sample UID FETCH results, then point Thunderbird to the hosted endpoint. Keep the source backup for rollback. Do not run the old and new servers against independently evolving identity databases after cutover.

Cloud snapshots must be restored deliberately; never initialize over a lost production database. Changing API namespace or inbox ID changes identity scope, even when an email address looks the same.

## Validation

`IMAP_TEST_POSTGRES_DSN` enables integration tests against a **dedicated empty** PostgreSQL database. The test suite refuses to overwrite an existing schema and leaves test tables in place for inspection. Tests cover concurrent UID allocation, retirement/restoration, restart stability, stale revision rejection, shared cooldown/pacing, snapshots/recent and whole-scan leases, cancellation and expired-owner fencing. Without that DSN, tests are explicitly skipped: syntax/unit tests do not establish real PostgreSQL correctness.

