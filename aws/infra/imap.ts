import * as aws from "@pulumi/aws";
import * as pulumi from "@pulumi/pulumi";
import { packageApplication } from "./package-application";
import { buildUserData } from "./ec2-bootstrap";
const c = new pulumi.Config("agentmail-imap-aws");
const hostname = c.require("hostname");
const assignedDomain =
  pulumi.getProject() === "imap-template"
    ? new pulumi.Config("imap-template").require("domain")
    : undefined;
if (
  assignedDomain &&
  (hostname === assignedDomain || !hostname.endsWith("." + assignedDomain))
)
  throw new Error(
    "Trial hostname must be a separate subdomain of the existing website domain",
  );
const zoneId =
  c.get("hostedZoneId") ??
  (assignedDomain
    ? aws.route53.getZoneOutput({ name: assignedDomain, privateZone: false })
        .zoneId
    : c.require("hostedZoneId"));
const enabled = c.getBoolean("enableService") ?? true;
// EC2 instances run Python directly; no image registry or container scheduler.
const desiredInstances = c.getNumber("desiredInstances") ?? 2;
const minInstances = c.getNumber("minInstances") ?? 2;
const maxInstances = c.getNumber("maxInstances") ?? 8;
const autoscaling = c.getBoolean("autoscaling") ?? true;
const instanceType = c.get("instanceType") ?? "t3.small";
if (!/^[a-z][a-z0-9]*\.[a-z0-9]+$/.test(instanceType))
  throw new Error("Invalid instanceType");
const maxConnections = c.getNumber("maxConnectionsPerInstance") ?? 100;
for (const [name, value] of Object.entries({
  desiredInstances,
  minInstances,
  maxInstances,
  maxConnections,
})) {
  if (!Number.isSafeInteger(value) || value < 1)
    throw new Error(`${name} must be a positive integer`);
}
if (
  minInstances > maxInstances ||
  desiredInstances < minInstances ||
  desiredInstances > maxInstances
)
  throw new Error(
    "Instance counts require minInstances <= desiredInstances <= maxInstances",
  );
const cpuTarget = c.getNumber("cpuTarget") ?? 60;
if (!Number.isFinite(cpuTarget) || cpuTarget <= 0 || cpuTarget >= 100)
  throw new Error("cpuTarget must be between 0 and 100");
const natCount = c.getNumber("natGateways") ?? 2;
if (![1, 2].includes(natCount)) throw new Error("natGateways must be 1 or 2");
const protect = c.getBoolean("protectDatabase") ?? true;
const tags = { Project: "agentmail-imap", Stack: pulumi.getStack() };
const region = aws.getRegionOutput();
const azs = aws
  .getAvailabilityZonesOutput({ state: "available" })
  .names.apply((a) => {
    if (a.length < 2) throw new Error("Two AZs required");
    return a.slice(0, 2);
  });
const vpc = new aws.ec2.Vpc(
  "imap",
  {
    cidrBlock: "10.42.0.0/16",
    enableDnsSupport: true,
    enableDnsHostnames: true,
    tags,
  },
  {},
);
const igw = new aws.ec2.InternetGateway("imap", { vpcId: vpc.id, tags });
const publicRoutes = new aws.ec2.RouteTable("public", {
  vpcId: vpc.id,
  routes: [{ cidrBlock: "0.0.0.0/0", gatewayId: igw.id }],
  tags,
});
const publicSubnets: aws.ec2.Subnet[] = [],
  appSubnets: aws.ec2.Subnet[] = [],
  dbSubnets: aws.ec2.Subnet[] = [],
  nats: aws.ec2.NatGateway[] = [],
  allocationIds: pulumi.Output<string>[] = [];
for (let i = 0; i < 2; i++) {
  const subnet = new aws.ec2.Subnet(`public-${i}`, {
    vpcId: vpc.id,
    availabilityZone: azs.apply((a) => a[i]),
    cidrBlock: `10.42.${i}.0/24`,
    tags,
  });
  publicSubnets.push(subnet);
  new aws.ec2.RouteTableAssociation(`public-${i}`, {
    subnetId: subnet.id,
    routeTableId: publicRoutes.id,
  });
  appSubnets.push(
    new aws.ec2.Subnet(`app-${i}`, {
      vpcId: vpc.id,
      availabilityZone: azs.apply((a) => a[i]),
      cidrBlock: `10.42.${10 + i}.0/24`,
      tags,
    }),
  );
  dbSubnets.push(
    new aws.ec2.Subnet(`db-${i}`, {
      vpcId: vpc.id,
      availabilityZone: azs.apply((a) => a[i]),
      cidrBlock: `10.42.${20 + i}.0/24`,
      tags,
    }),
  );
  if (i < natCount) {
    const ip = new aws.ec2.Eip(`nat-${i}`, { domain: "vpc", tags });
    allocationIds.push(ip.id);
    nats.push(
      new aws.ec2.NatGateway(
        `nat-${i}`,
        { allocationId: ip.id, subnetId: subnet.id, tags },
        { dependsOn: [igw] },
      ),
    );
  }
}
const appRouteAssociations: aws.ec2.RouteTableAssociation[] = [];
for (let i = 0; i < 2; i++) {
  const route = new aws.ec2.RouteTable(`app-${i}`, {
    vpcId: vpc.id,
    routes: [{ cidrBlock: "0.0.0.0/0", natGatewayId: nats[i % natCount].id }],
    tags,
  });
  appRouteAssociations.push(
    new aws.ec2.RouteTableAssociation(`app-${i}`, {
      subnetId: appSubnets[i].id,
      routeTableId: route.id,
    }),
  );
  const dbRoute = new aws.ec2.RouteTable(`db-${i}`, { vpcId: vpc.id, tags });
  new aws.ec2.RouteTableAssociation(`db-${i}`, {
    subnetId: dbSubnets[i].id,
    routeTableId: dbRoute.id,
  });
}
const nlbSg = new aws.ec2.SecurityGroup("nlb", {
  vpcId: vpc.id,
  ingress: [
    {
      protocol: "tcp",
      fromPort: 993,
      toPort: 993,
      cidrBlocks: c.getObject<string[]>("clientCidrs") ?? ["0.0.0.0/0"],
    },
  ],
  egress: [
    {
      protocol: "tcp",
      fromPort: 1143,
      toPort: 1143,
      cidrBlocks: ["10.42.0.0/16"],
    },
    {
      protocol: "tcp",
      fromPort: 8080,
      toPort: 8080,
      cidrBlocks: ["10.42.0.0/16"],
    },
  ],
  tags,
});
const instanceSg = new aws.ec2.SecurityGroup("instances", {
  vpcId: vpc.id,
  ingress: [
    {
      protocol: "tcp",
      fromPort: 1143,
      toPort: 1143,
      securityGroups: [nlbSg.id],
    },
    {
      protocol: "tcp",
      fromPort: 8080,
      toPort: 8080,
      securityGroups: [nlbSg.id],
    },
  ],
  egress: [
    { protocol: "tcp", fromPort: 443, toPort: 443, cidrBlocks: ["0.0.0.0/0"] },
    { protocol: "tcp", fromPort: 80, toPort: 80, cidrBlocks: ["0.0.0.0/0"] },
    {
      protocol: "tcp",
      fromPort: 5432,
      toPort: 5432,
      cidrBlocks: ["10.42.20.0/24", "10.42.21.0/24"],
    },
  ],
  tags,
});
const dbSg = new aws.ec2.SecurityGroup("database", {
  vpcId: vpc.id,
  ingress: [
    {
      protocol: "tcp",
      fromPort: 5432,
      toPort: 5432,
      securityGroups: [instanceSg.id],
    },
  ],
  tags,
});
const password = c.requireSecret("databasePassword").apply((value) => {
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(value))
    throw new Error(
      "databasePassword must contain 16–128 letters, digits, underscores or hyphens",
    );
  return value;
});
const subnetGroup = new aws.rds.SubnetGroup("imap", {
  subnetIds: dbSubnets.map((s) => s.id),
  tags,
});
const params = new aws.rds.ParameterGroup("imap", {
  family: "postgres16",
  parameters: [{ name: "rds.force_ssl", value: "1" }],
  tags,
});
const db = new aws.rds.Instance(
  "imap",
  {
    engine: "postgres",
    engineVersion: c.get("postgresVersion") ?? "16",
    instanceClass: c.get("databaseClass") ?? "db.t4g.small",
    allocatedStorage: 20,
    maxAllocatedStorage: 100,
    storageType: "gp3",
    storageEncrypted: true,
    dbName: "imap",
    username: "imap",
    password: password,
    port: 5432,
    multiAz: true,
    dbSubnetGroupName: subnetGroup.name,
    vpcSecurityGroupIds: [dbSg.id],
    parameterGroupName: params.name,
    publiclyAccessible: false,
    backupRetentionPeriod: 7,
    deletionProtection: protect,
    skipFinalSnapshot: true,
    finalSnapshotIdentifier: `imap-${pulumi.getStack()}-final`,
    autoMinorVersionUpgrade: true,
    applyImmediately: false,

    tags,
  },
  { protect },
);
const artifact = packageApplication();
const artifactBucket = new aws.s3.Bucket("imap-artifacts", {
  forceDestroy: true,
  tags,
});
const artifactBlock = new aws.s3.BucketPublicAccessBlock("imap-artifacts", {
  bucket: artifactBucket.id,
  blockPublicAcls: true,
  blockPublicPolicy: true,
  ignorePublicAcls: true,
  restrictPublicBuckets: true,
});
const artifactEncryption = new aws.s3.BucketServerSideEncryptionConfiguration(
  "imap-artifacts",
  {
    bucket: artifactBucket.id,
    rules: [{ applyServerSideEncryptionByDefault: { sseAlgorithm: "AES256" } }],
  },
);
const artifactObject = new aws.s3.BucketObjectv2(
  "imap-application",
  {
    bucket: artifactBucket.id,
    key: artifact.key,
    source: new pulumi.asset.FileAsset(artifact.path),
    sourceHash: artifact.sha256,
  },
  { dependsOn: [artifactBlock, artifactEncryption] },
);
const role = new aws.iam.Role("instance", {
  assumeRolePolicy: JSON.stringify({
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Principal: { Service: "ec2.amazonaws.com" },
        Action: "sts:AssumeRole",
      },
    ],
  }),
  tags,
});
const instancePolicy = new aws.iam.RolePolicy("instance", {
  role: role.id,
  policy: artifactBucket.arn.apply((bucket) =>
    JSON.stringify({
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Action: ["s3:GetObject"],
          Resource: [
            `${bucket}/${artifact.key}`,
            `${bucket}/configuration/runtime.env`,
          ],
        },
      ],
    }),
  ),
});
const profile = new aws.iam.InstanceProfile("imap", { role: role.name, tags });
const lb = new aws.lb.LoadBalancer("imap", {
  loadBalancerType: "network",
  internal: false,
  subnets: publicSubnets.map((s) => s.id),
  securityGroups: [nlbSg.id],
  enableCrossZoneLoadBalancing: true,
  tags,
});
const target = new aws.lb.TargetGroup("imap", {
  vpcId: vpc.id,
  targetType: "instance",
  port: 1143,
  protocol: "TCP",
  deregistrationDelay: 30,
  healthCheck: {
    protocol: "HTTP",
    port: "8080",
    path: "/ready",
    matcher: "200",
    interval: 30,
    healthyThreshold: 2,
    unhealthyThreshold: 2,
  },
  tags,
});
const cert = new aws.acm.Certificate("imap", {
  domainName: hostname,
  validationMethod: "DNS",
  tags,
});
const validationRecord = new aws.route53.Record("cert", {
  zoneId,
  name: cert.domainValidationOptions[0].resourceRecordName,
  type: cert.domainValidationOptions[0].resourceRecordType,
  records: [cert.domainValidationOptions[0].resourceRecordValue],
  ttl: 60,
});
const validation = new aws.acm.CertificateValidation("imap", {
  certificateArn: cert.arn,
  validationRecordFqdns: [validationRecord.fqdn],
});
const listener = new aws.lb.Listener("imap", {
  loadBalancerArn: lb.arn,
  port: 993,
  protocol: "TLS",
  sslPolicy: "ELBSecurityPolicy-TLS13-1-2-2021-06",
  certificateArn: validation.certificateArn,
  defaultActions: [{ type: "forward", targetGroupArn: target.arn }],
});
new aws.route53.Record("imap", {
  zoneId,
  name: hostname,
  type: "A",
  aliases: [
    { name: lb.dnsName, zoneId: lb.zoneId, evaluateTargetHealth: true },
  ],
});
const environmentKey = "configuration/runtime.env";
const environmentObject = new aws.s3.BucketObjectv2(
  "imap-environment",
  {
    bucket: artifactBucket.id,
    key: environmentKey,
    content: pulumi.all([db.address, password]).apply(
      ([host, dbPassword]) =>
        Object.entries({
          IMAP_HOST: "0.0.0.0",
          IMAP_PORT: "1143",
          IMAP_TLS_MODE: "terminated",
          IMAP_DB_HOST: host,
          IMAP_DB_PORT: "5432",
          IMAP_DB_NAME: "imap",
          IMAP_DB_USER: "imap",
          IMAP_DB_PASSWORD: dbPassword,
          IMAP_DB_SSLMODE: "verify-full",
          IMAP_DB_SSLROOTCERT: "/app/certs/global-bundle.pem",
          IMAP_HEALTH_PORT: "8080",
          AGENTMAIL_API_URL: "https://api.agentmail.to/v0",
          IMAP_STORAGE_BACKEND: "postgres",
          IMAP_DRAIN_SECONDS: "100",
          IMAP_MAX_CONNECTIONS: String(maxConnections),
          IMAP_API_REQUESTS_PER_SECOND: String(
            c.getNumber("apiRequestsPerSecond") ?? 5,
          ),
          IMAP_ASSUME_FULL_VISIBILITY: String(
            c.getBoolean("assumeFullVisibility") ?? false,
          ),
        })
          .map(([name, value]) => {
            if (/[\r\n\0]/.test(value))
              throw new Error("Invalid runtime environment value");
            return `${name}=${value}`;
          })
          .join("\n") + "\n",
    ),
    serverSideEncryption: "AES256",
  },
  { dependsOn: [artifactBlock, artifactEncryption] },
);
const groupName = `imap-${pulumi.getProject()}-${pulumi.getStack()}`;
const ami =
  c.get("amiId") ??
  aws.ssm.getParameterOutput({
    name: "/aws/service/canonical/ubuntu/server/noble/stable/current/amd64/hvm/ebs-gp3/ami-id",
  }).value;
const launch = new aws.ec2.LaunchTemplate(
  "imap",
  {
    imageId: ami,
    instanceType,
    iamInstanceProfile: { name: profile.name },
    vpcSecurityGroupIds: [instanceSg.id],
    metadataOptions: {
      httpEndpoint: "enabled",
      httpTokens: "required",
      httpPutResponseHopLimit: 1,
    },
    blockDeviceMappings: [
      {
        deviceName: "/dev/sda1",
        ebs: {
          volumeSize: 16,
          volumeType: "gp3",
          encrypted: "true",
          deleteOnTermination: "true",
        },
      },
    ],
    creditSpecification: instanceType.startsWith("t3.")
      ? { cpuCredits: "standard" }
      : undefined,
    tagSpecifications: [
      { resourceType: "instance", tags },
      { resourceType: "volume", tags },
    ],
    userData: pulumi
      .all([region.name, artifactBucket.id, environmentObject.etag])
      .apply(([regionName, bucket, environmentEtag]) => {
        const script =
          buildUserData({
            region: regionName,
            bucket,
            key: artifact.key,
            sha256: artifact.sha256,
            imageTag: artifact.imageTag,
            environmentKey,
          }) + `\n# Runtime configuration revision: ${environmentEtag}\n`;
        if (Buffer.byteLength(script) > 16384)
          throw new Error("EC2 bootstrap exceeds 16KiB user-data limit");
        return Buffer.from(script).toString("base64");
      }),
    tags,
  },
  { dependsOn: [artifactObject, environmentObject, instancePolicy, db] },
);
const group = new aws.autoscaling.Group(
  "imap",
  {
    name: groupName,
    minSize: enabled ? minInstances : 0,
    maxSize: enabled ? maxInstances : 0,
    desiredCapacity: enabled ? desiredInstances : 0,
    vpcZoneIdentifiers: appSubnets.map((s) => s.id),
    launchTemplate: {
      id: launch.id,
      version: launch.latestVersion.apply(String),
    },
    targetGroupArns: [target.arn],
    healthCheckType: "ELB",
    healthCheckGracePeriod: 600,
    defaultInstanceWarmup: 300,
    waitForCapacityTimeout: "20m",
    minElbCapacity: enabled ? minInstances : 0,
    instanceRefresh: {
      strategy: "Rolling",
      preferences: {
        minHealthyPercentage: 100,
        maxHealthyPercentage: 150,
        instanceWarmup: "300",
      },
    },
    tags: Object.entries(tags).map(([key, value]) => ({
      key,
      value,
      propagateAtLaunch: true,
    })),
  },
  {
    dependsOn: [listener, ...nats, ...appRouteAssociations],
    ignoreChanges: enabled && autoscaling ? ["desiredCapacity"] : [],
  },
);
const scalingPolicies: aws.autoscaling.Policy[] = [];
if (enabled && autoscaling) {
  scalingPolicies.push(
    new aws.autoscaling.Policy("imap-cpu", {
      autoscalingGroupName: group.name,
      policyType: "TargetTrackingScaling",
      estimatedInstanceWarmup: 300,
      targetTrackingConfiguration: {
        targetValue: cpuTarget,
        predefinedMetricSpecification: {
          predefinedMetricType: "ASGAverageCPUUtilization",
        },
      },
    }),
  );
}
export const imapHostname = hostname;
export const autoScalingGroupName = group.name;
export const launchTemplateId = launch.id;
export const artifactBucketName = artifactBucket.id;
export const applicationSha256 = artifact.sha256;
export const privateSubnetIds = appSubnets.map((s) => s.id);
export const instanceSecurityGroupId = instanceSg.id;
export const databaseEndpoint = db.address;
export const serviceEnabled = enabled;
export const scaling = {
  desiredInstances,
  minInstances,
  maxInstances,
  autoscaling,
  instanceType,
  maxConnectionsPerInstance: maxConnections,
};

export const autoscalingPolicyArns = scalingPolicies.map(
  (policy) => policy.arn,
);
