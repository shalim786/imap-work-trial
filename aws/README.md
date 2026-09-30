# AWS AgentMail IMAP implementation

This package is a complete independent copy of the application. It runs on an EC2 Auto Scaling Group behind an ACM TLS Network Load Balancer, with shared Multi-AZ PostgreSQL identities and per-instance temporary raw caches.

Pulumi builds a Docker image locally, saves a content-addressed archive to private S3, and provisions systemd-managed instances. There is no ECR/ECS/Fargate deployment, centralized log shipping, custom memory telemetry, scheduled expiry or budget-triggered cleanup. Docker must be installed and running on the operator machine.

Read [DEPLOYMENT.md](DEPLOYMENT.md) for stack access, database secret configuration, one-command deployment and manual cleanup preserving the assigned stack's existing website. [STORAGE.md](STORAGE.md) describes identities and optional offline SQLite import. The runtime supplies fresh-database initialization with coordinated startup; it does not automatically import local state.

| Part | Location |
| --- | --- |
| Protocol, MIME, API adapter and request/cache policies | `agentmail_imap/` |
| Shared identity storage and coordination | `agentmail_imap/postgres_store.py` |
| EC2, image packaging, NLB, RDS and website-preserving wrapper | `infra/` |
| Container and verified RDS TLS | `Dockerfile`, `certs/` |
| Optional UID import | `scripts/migrate_uid_store.py` |

Client authentication is per-session inbox ID and AgentMail API key. Public port993 uses SSL/TLS; private TCP1143 is constrained by network security groups. Readiness on8080 checks storage. Refresh and request coordination use credential fingerprints, not raw API keys. Shared behavior fixes must be applied deliberately to both `local/` and `aws/`.

## Local checks without deployment

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests
cd infra
pnpm install --frozen-lockfile
pnpm run build
```

Synthetic tests use temporary loopback listeners. PostgreSQL tests require a dedicated disposable test database. Infrastructure mocks replace image packaging so checks need neither Docker nor AWS calls. Recorded [failure/load evidence](../docs/failure-load-evidence.md) is single-process synthetic evidence, not cloud capacity proof. TCP keepalive and idle protocol heartbeats do not alter literal boundaries or reset inactivity deadlines.

No AWS resources have been created. Hosted acceptance, image bootstrap, live PostgreSQL failover and scaling remain required. Codex generated/revised application, infrastructure, tests and documentation; the exercised slice does not claim complete IMAP compatibility. Sending, APPEND/drafts and arbitrary mailbox writes remain unsupported.
