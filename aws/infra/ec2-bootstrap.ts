/** Load the immutable application image from private S3 and run it on EC2. */
export interface BootstrapArgs {
  region: string;
  bucket: string;
  key: string;
  sha256: string;
  imageTag: string;
  environmentKey: string;
}

export function buildUserData(args: BootstrapArgs): string {
  const q = (value: string) => "'" + value.replace(/'/g, "'\\''") + "'";
  if (!/^[a-f0-9]{64}$/.test(args.sha256))
    throw new Error("Invalid application SHA256");
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$/.test(args.imageTag))
    throw new Error("Invalid image tag");
  return `#!/bin/bash
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io unzip curl ca-certificates
systemctl enable --now docker
curl --fail --silent --show-error https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
install -d -m 700 /etc/agentmail-imap
aws s3 cp ${q(`s3://${args.bucket}/${args.key}`)} /tmp/app-image.tar.gz --region ${q(args.region)} --only-show-errors
echo ${q(args.sha256 + "  /tmp/app-image.tar.gz")} | sha256sum --check --status
gzip -dc /tmp/app-image.tar.gz | docker load >/dev/null
docker image inspect ${q(args.imageTag)} >/dev/null
aws s3 cp ${q(`s3://${args.bucket}/${args.environmentKey}`)} /etc/agentmail-imap/environment --region ${q(args.region)} --only-show-errors
chmod 600 /etc/agentmail-imap/environment
cat > /etc/systemd/system/agentmail-imap.service <<'UNIT'
[Unit]
Description=AgentMail IMAP Docker server
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service
[Service]
Type=simple
ExecStartPre=/usr/bin/docker run --rm --network host --env-file /etc/agentmail-imap/environment ${args.imageTag} python /app/scripts/ensure_database.py
ExecStart=/usr/bin/docker run --rm --name agentmail-imap --network host --env-file /etc/agentmail-imap/environment --stop-timeout 110 --log-opt max-size=10m --log-opt max-file=3 ${args.imageTag}
ExecStop=/usr/bin/docker stop --time 110 agentmail-imap
Restart=on-failure
RestartSec=15
TimeoutStartSec=180
TimeoutStopSec=120
KillSignal=SIGTERM
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now agentmail-imap
rm -rf /tmp/app-image.tar.gz /tmp/aws /tmp/awscliv2.zip
`;
}
