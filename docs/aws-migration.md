# AWS setup and code map

The latest approved simplification uses **EC2 with systemd-managed Docker**, private S3 image archives, an NLB and shared Multi-AZ RDS. This supersedes the earlier Fargate/ECR and timed disposable-trial designs. No AWS resources have been created.

Use the existing **agentmail-org/imap-template/intern** stack and ESC environment **platform/intern**. The website remains at imap.to; IMAP uses **trial-imap.imap.to**. Preview must preserve the website's resource identities and outputs. Never destroy the shared stack.

## Your setup

1. Obtain Pulumi stack/ESC access and confirm the AWS account, us-east-1 region and deployment-role permissions.
2. Install Pulumi, Node22+, pnpm11.9.0 and Docker with a running engine.
3. Select the assigned stack, install the infrastructure dependencies and enter the database password through `pulumi config set --secret agentmail-imap-aws:databasePassword` using its prompt.
4. Inspect `pulumi preview`, then run **`pulumi up`**. Pulumi builds/saves the image, uploads the private hashed archive, and provisions/boots the application. No manual AWS CLI container setup is required.
5. Validate Thunderbird on993/SSL-TLS/Normal password, using the inbox ID and its AgentMail API key.

See [the full deployment guide](../aws/DEPLOYMENT.md) for commands, IAM/DNS prerequisites, secret exposure, acceptance and cleanup. Pulumi encrypted configuration does not make runtime configuration a secret vault: authorized readers of the private S3 runtime environment object can retrieve the database credential. Avoid secret command arguments and limit operator permissions.

The ASG defaults to two instances across two AZs, CPU target60%, maximum eight, t3.small and100 connections per instance. Standard CPU scaling is implemented; memory and active-connection custom metrics are absent. Public TLS terminates at the NLB; private application traffic is constrained by security groups; PostgreSQL TLS verifies the server certificate. Two NAT gateways provide outbound API access.

## Code placement

| Responsibility | Local | AWS |
| --- | --- | --- |
| Protocol/MIME/flags/API client | `local/agentmail_imap/` | Independent `aws/agentmail_imap/` copy |
| Identity storage | SQLite `uid_store.py` | Shared PostgreSQL `postgres_store.py` |
| Runtime/readiness | Local executable | AWS executable, fresh-database setup and health endpoint |
| Raw cache | Local temporary cache | Per-instance temporary cache |
| Packaging | Not required | Dockerfile and hashed private S3 archive |
| Infrastructure | None | `aws/infra/index.ts` wrapper, website preservation and application module |
| Identity preservation | Consistent local SQLite backup | Offline `aws/scripts/migrate_uid_store.py` import with private DB access |
| Settings | Private local environment/Thunderbird | Pulumi secret database setting and per-session Thunderbird API key |

No credentials, local SQLite files or raw caches belong in the Docker build context. Copies are deliberately redundant; apply shared fixes to both trees and verify independently. The fresh hosted database automatically initializes under coordination, but local identities are not imported. Preserving them requires a planned offline backup/import before hosted writes; consult [STORAGE.md](../aws/STORAGE.md). Never restore an old SQLite snapshot over identities that have advanced in PostgreSQL.

## Cost and cleanup

The earlier $200/24-hour automation has been removed. There is no enforced expiry, cloud budget action, cleanup Lambda/Scheduler, centralized application logs or custom alarms. Infrastructure remains billable until manual removal.

Set `enableService=false` and apply to stop instances. If removing the database, set `protectDatabase=false` and apply. Then set `appEnabled=false`, preview that the website remains, and apply. **Do not use whole-stack `pulumi destroy`.** Backup hosted UID state first if recovery is required. AgentMail inboxes and local SQLite remain untouched.

## Validation boundary

Offline TypeScript/Pulumi checks and synthetic Python/Node tests do not establish real image/bootstrap, IAM, DNS, RDS failover or cloud capacity. Validate the actual deployment's two-instance behavior, reconnect identity stability, read/unread state, trash filtering, new arrivals/removals, bodies and attachments. No AWS deployment occurred during preparation.
