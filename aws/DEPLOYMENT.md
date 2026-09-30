# AWS setup and deployment

The AWS package is independent of `local/`. This preparation has created no AWS resources. The assigned stack is **agentmail-org/imap-template/intern**, which already owns the imap.to website. The combined Pulumi program preserves the website and adds the IMAP application at **trial-imap.imap.to**. Always inspect preview for unexpected website changes; never destroy the shared stack.

## Architecture and code placement

A public Network Load Balancer terminates ACM TLS on port 993 and forwards private TCP1143 to an EC2 Auto Scaling Group. The group starts with two instances across two Availability Zones and scales between two and eight using standard EC2 CPU utilization, targeting 60%. Instances retrieve a private, content-addressed Docker image archive from S3 and run it under systemd. Pulumi builds and saves that archive locally; there is no ECR, ECS or Fargate deployment.

A shared encrypted PostgreSQL RDS Multi-AZ primary/standby preserves UIDs, reconciliation state and credential request budgets across application instances. The standby provides failover rather than a second writable primary. Database connections use verified TLS. Raw message caches are bounded, temporary and instance-local. Two NAT gateways provide private-instance egress to AgentMail and signed message download URLs. TLS terminates at the NLB; the NLB-to-application leg is private plaintext constrained by security groups.

| Responsibility | Location |
| --- | --- |
| IMAP protocol, AgentMail client, cache and request policy | `aws/agentmail_imap/` |
| Shared UID persistence | `aws/agentmail_imap/postgres_store.py` |
| Image build/package and EC2/systemd bootstrap | `aws/infra/` |
| Docker runtime and public RDS CA bundle | `aws/Dockerfile`, `aws/certs/` |
| Existing website preservation | `aws/infra/website.ts`, wrapper `aws/infra/index.ts` |
| Optional offline SQLite identity import | `aws/scripts/migrate_uid_store.py` |

The application initializes a fresh database under coordinated startup. This does not import local UID history. A fresh hosted database has a new UIDVALIDITY, so Thunderbird rebuilds its server cache. If existing mappings must be preserved, arrange a consistent SQLite backup and private database import **before allowing hosted mailbox writes**; see [STORAGE.md](STORAGE.md). Do not run independently writable local/cloud endpoints as if they share one identity store.

## What you need to provide

- Access to `agentmail-org/imap-template/intern` and the existing Pulumi ESC environment `platform/intern`; confirm the intended AWS account and `us-east-1` region.
- Permission to use `trial-imap.imap.to` and add its certificate validation and endpoint records in the existing public hosted zone.
- An operator-selected database password, entered through Pulumi's secret prompt below. Do not paste it into chat or place it in a command argument.
- Approval for the resource costs, and confirmation whether tester IP ranges should restrict the public listener.
- Hosted UID mappings start fresh, as requested; no local mapping import is performed.

The simplified deployment has **no automatic 24-hour expiry, cleanup schedule, budget actions, centralized application logs, custom memory metrics or alarms**. The previously discussed $200/24-hour trial controls are removed. Resources remain billable until you carry out the manual cleanup. Set your own reminder and use the AWS billing console to observe charges; no hard budget ceiling is implemented.

## Prepare your workstation

Install Docker with a running engine, Pulumi CLI, Node 22+, pnpm 11.9.0 and Python for optional operator scripts. Authenticate to Pulumi and confirm your authorized ESC role can create VPC/NAT/EC2/Auto Scaling/IAM/S3/NLB/ACM/Route53/RDS resources. The stack imports the existing ESC credentials; no manual AWS CLI deployment steps are required.

```sh
cd aws/infra
pulumi login
pulumi stack select agentmail-org/imap-template/intern
pnpm install --frozen-lockfile
pnpm run build
pulumi config set --secret agentmail-imap-aws:databasePassword
pnpm test
pulumi preview
```

The secret command prompts for the password. Use 16–128 characters consisting of letters, digits, underscores or hyphens. Pulumi encrypts it in configuration/state, but EC2 bootstrap must receive the credential: **users/roles allowed to read the private runtime environment object in S3 can retrieve it**. The runtime environment object is private and encrypted at rest in S3; bootstrap downloads it into a restricted local file without logging the password. Encrypted Pulumi storage is not a runtime secret vault. Limit S3 configuration-object read permissions and host access accordingly.

Confirm the existing domain configuration, hostname, AWS account, instance sizing and resource preview before applying. The application defaults to `t3.small`, desired/minimum two and maximum eight instances, and 100 connections per instance. These are starting limits, not measured cloud capacity. CPU scaling cannot detect an idle connection-cap bottleneck; existing TCP sessions do not migrate when the group scales out.

## Deploy

With a working Docker engine, package dependencies and authorized stack configuration ready, one command builds/packages the application and provisions its dependencies:

```sh
pulumi up
```

Pulumi uploads the private content-addressed image archive to S3, provisions EC2 launch configuration and scaling, and enables the systemd-managed application. No image registry push, manual container setup, one-off ECS task, or second CLI service-enable phase is required. Provisioning can take time for RDS, certificate validation and instance bootstrap. Inspect Pulumi outputs and AWS instance/systemd status if readiness does not become healthy; deployment completion is not a substitute for client acceptance.

For subsequent code changes, run the same preview/update sequence. Content hashes select a new image archive and launch configuration. Existing client connections can disconnect during rolling replacement and should reconnect with unchanged PostgreSQL identities.

## Thunderbird acceptance

Use hostname **trial-imap.imap.to**, port **993**, **SSL/TLS**, and **Normal password**. The username is the AgentMail inbox ID/email; the password is that inbox's authorized AgentMail API key. There is no global server AgentMail key. Keys need read/update and the relevant protected-label permissions. Full-visibility assertion defaults false; enable it only when every admitted key has appropriate visibility.

Check both configured accounts, pagination, bodies and attachments, read/unread roundtrip, trash exclusion, new arrivals and removals on refresh, reconnecting across instances, and UID stability after application replacement. Validate readiness and RDS connectivity, and test instance/database failover before claiming production compatibility. Inspect local instance logs through authorized host access; centralized log shipping is not configured.

## Configurable capacity

`desiredInstances`, `minInstances`, `maxInstances` default to 2/2/8; `instanceType` defaults `t3.small`; `maxConnectionsPerInstance` defaults 100; `cpuTarget` defaults 60. `autoscaling` enables CPU target tracking. Disabling the service must override desired/minimum capacity to zero so scaling cannot restore it. PostgreSQL/API coordination adds overhead, and adding instances does not multiply the per-credential AgentMail rate budget.

## Manual cleanup preserving the website

Run from the assigned stack in `aws/infra`. Stop application capacity first:

```sh
pulumi config set agentmail-imap-aws:enableService false
pulumi up
```

If you intend to delete the hosted UID database, disable its deletion protection explicitly:

```sh
pulumi config set agentmail-imap-aws:protectDatabase false
pulumi up
```

Then remove only the application module:

```sh
pulumi config set agentmail-imap-aws:appEnabled false
pulumi preview
pulumi up
```

Verify preview retains the website's S3/CloudFront resources and outputs. Application deletion removes its database and networking/storage resources according to their configured deletion policies; take an approved database backup first if UID recovery is required. AgentMail messages and local SQLite are not deleted by this sequence. **Never use `pulumi destroy` on this shared stack.**

## Validation boundary

Offline Pulumi mocks and TypeScript checks validate resource declarations without AWS calls. Python/Node tests exercise synthetic transport and storage behavior. Image builds require Docker; hosted bootstrap, PostgreSQL integration, DNS/TLS, scaling, replacement and failover require the actual authorized environment. No AWS resources were deployed during preparation.
