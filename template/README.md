# imap-template

Minimal Pulumi (TypeScript) starter: a static page in `www/`, stored in a private S3 bucket, served over HTTPS by CloudFront at a Route53 domain.

```
Route53 (A/AAAA alias) → CloudFront (ACM cert, HTTPS) → S3 (private, Origin Access Control)
```

## Prerequisites

- A public Route53 hosted zone for your domain in the target AWS account, with the registrar delegated to it.
- Pulumi CLI, Node 22+, pnpm.

## Deploy

```bash
pnpm install
pulumi stack select agentmail-org/intern   # or: pulumi stack init <org>/<stack>
pulumi up
```

`Pulumi.intern.yaml` pulls AWS credentials from the `platform/intern` Pulumi ESC environment and sets `domain: imap.to`. For a new stack, set your own:

```bash
pulumi config set aws:region us-east-1
pulumi config set domain example.com
```

Edit files in `www/` and run `pulumi up` again to publish. CloudFront caches for up to a day; run `aws cloudfront create-invalidation --distribution-id $(pulumi stack output distributionId) --paths '/*'` to see changes immediately.
