# AgentMail IMAP bridge

[ABHINAVCHOICES.md](ABHINAVCHOICES.md) documents the project, each function, the requests and decisions from this chat, and known tradeoffs. [Failure/load evidence](docs/failure-load-evidence.md) records repeatable checks and their limits.

The repository has two self-contained implementations. Application code is intentionally duplicated so each version can be inspected independently.

- **[Local version](local/README.md):** the working SQLite-backed service and its synthetic tests. Existing private configuration and UID state are now in `local/.env`, `local/.env.mail_test`, and `local/var/`. The running listener remains `127.0.0.1:1143`.
- **[AWS version](aws/README.md):** application container, PostgreSQL identity store, cross-worker coordination, and Pulumi infrastructure for autoscaled EC2 Docker instances, NLB TLS, and Multi-AZ RDS. Start with [deployment instructions](aws/DEPLOYMENT.md).
- **[AWS setup and migration checklist](docs/aws-migration.md):** access requirements, decisions, and module responsibilities.

`aws/website-template/` preserves the original S3/CloudFront starter separately from the IMAP infrastructure in `aws/infra/`.

No IMAP containers or AWS resources have been started by this work. The assigned stack already hosts the imap.to website; the new code preserves it. Container builds, real PostgreSQL integration, and hosted acceptance must pass before the AWS version can be called deployment-verified.

## Running the existing local service

```sh
cd local
# Use the existing workspace virtual environment, or follow local/README.md to create one.
../.venv/bin/python -m agentmail_imap
```

Do not initialize/reset the existing UID database when restarting. Local and AWS copies are not automatically synchronized; port a behavior fix deliberately to both and run each suite.

Meaningful application, test, infrastructure, and documentation code was generated or revised with Codex and reviewed through the recorded checks. No IMAP server/protocol-core library is used.

The AWS deployment uses `agentmail-org/imap-template/intern`, `trial-imap.imap.to`, EC2 scaling from two to eight instances, a Docker archive in private S3, an NLB and shared PostgreSQL. Budget alerts and automatic expiry/cleanup were removed by user decision; teardown is manual. See [deployment and cleanup](aws/DEPLOYMENT.md).
