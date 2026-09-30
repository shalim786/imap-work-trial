// Offline contract checks; provider and Docker packaging are mocked.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const ts = require("typescript");
const pulumi = require("@pulumi/pulumi");
const resources = [];
const mode = process.argv[2] ?? "bootstrap";
const enabled = mode !== "stopped";
const scaled = mode === "scaled";
const extra = {
  "assigned-disabled": { appEnabled: "false" },
  stopped: { enableService: "false" },
  scaled: {
    desiredInstances: "3",
    minInstances: "2",
    maxInstances: "6",
    maxConnectionsPerInstance: "250",
    cpuTarget: "55",
    instanceType: "t3.medium",
  },
  "invalid-counts": { minInstances: "4", desiredInstances: "2" },
  "invalid-target": { cpuTarget: "100" },
  "invalid-instance": { instanceType: "bad/instance" },
  "invalid-connections": { maxConnectionsPerInstance: "0" },
  "invalid-nat": { natGateways: "3" },
};
pulumi.runtime.setAllConfig({
  "imap-template:domain": "imap.to",
  "agentmail-imap-aws:hostname": mode.startsWith("assigned")
    ? "trial-imap.imap.to"
    : "imap.example.test",
  "agentmail-imap-aws:hostedZoneId": "ZEXAMPLE",
  "agentmail-imap-aws:databasePassword": "synthetic-db-password-123",
  ...Object.fromEntries(
    Object.entries(extra[mode] ?? {}).map(([k, v]) => [
      `agentmail-imap-aws:${k}`,
      v,
    ]),
  ),
});
pulumi.runtime.setMocks(
  {
    newResource(args) {
      resources.push(args);
      return {
        id: `${args.name}-id`,
        state: {
          ...args.inputs,
          name: args.name,
          identifier: args.name,
          arn: `arn:aws:mock:us-east-1:123456789012:${args.name}`,
          address: "database.example.test",
          dnsName: "nlb.example.test",
          zoneId: "ZNLB",
          arnSuffix: args.name,
          latestVersion: 1,
          etag: "synthetic-etag",
          bucket: args.name,
          certificateArn: "arn:aws:acm:mock:certificate",
          domainValidationOptions: [
            {
              resourceRecordName: "_validation.example.test",
              resourceRecordType: "CNAME",
              resourceRecordValue: "_validation.acm.example.test",
            },
          ],
        },
      };
    },
    call(args) {
      if (args.token === "aws:index/getAvailabilityZones:getAvailabilityZones")
        return { names: ["us-east-1a", "us-east-1b"] };
      if (args.token === "aws:index/getCallerIdentity:getCallerIdentity")
        return { accountId: "123456789012" };
      if (args.token === "aws:route53/getZone:getZone")
        return { zoneId: "ZWEBSITE", name: "imap.to" };
      if (args.token === "aws:index/getRegion:getRegion")
        return { name: "us-east-1" };
      if (args.token === "aws:iam/getPolicyDocument:getPolicyDocument")
        return { json: "{}" };
      if (args.token === "aws:ssm/getParameter:getParameter")
        return { value: "ami-synthetic" };
      throw Error(`Unexpected provider call ${args.token}`);
    },
  },
  mode.startsWith("assigned") ? "imap-template" : "agentmail-imap-aws",
  "test",
  false,
);
require.extensions[".ts"] = (mod, filename) => {
  if (path.basename(filename) === "package-application.ts") {
    mod.exports = {
      packageApplication: () => ({
        imageTag: "agentmail-imap:synthetic",
        path: "/tmp/imap-synthetic-image.tar.gz",
        sha256: "a".repeat(64),
        key: "images/" + "a".repeat(64) + ".tar.gz",
      }),
    };
    return;
  }
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  mod._compile(code, filename);
};
const monitor = require("@pulumi/pulumi/runtime/settings").getMonitor();
const register = monitor.registerResource.bind(monitor);
let ignored;
monitor.registerResource = (request, callback) => {
  if (request.getType() === "aws:autoscaling/group:Group")
    ignored = request.getIgnorechangesList();
  return register(request, callback);
};
(async () => {
  await pulumi.runtime.runInPulumiStack(async () => {
    const filename = path.join(__dirname, "index.ts");
    const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    const compiled = new Module(filename, module);
    compiled.filename = filename;
    compiled.paths = Module._nodeModulePaths(__dirname);
    compiled._compile(code, filename);
    await Promise.all(
      Object.values(compiled.exports).map((o) => pulumi.output(o).promise()),
    );
  });
  await pulumi.runtime.disconnect();
  const ofType = (t) => resources.filter((r) => r.type === t);
  if (mode.startsWith("assigned")) {
    assert.equal(
      ofType("aws:s3/bucket:Bucket").find((r) => r.name === "site").name,
      "site",
    );
    assert.equal(
      ofType("aws:cloudfront/distribution:Distribution")[0].name,
      "site",
    );
    assert.equal(
      ofType("aws:route53/record:Record").filter((r) =>
        r.name.startsWith("site-"),
      ).length,
      3,
    );
  }
  if (mode === "assigned-disabled") {
    assert.equal(ofType("aws:rds/instance:Instance").length, 0);
    assert.equal(ofType("aws:autoscaling/group:Group").length, 0);
    assert.equal(ofType("aws:ec2/natGateway:NatGateway").length, 0);
    console.log("Assigned website remains when application is disabled.");
    return;
  }
  for (const r of resources)
    assert(
      !/aws:(ecs|ecr|budgets|scheduler|lambda|sns|sqs|cloudwatch)\//.test(
        r.type,
      ),
      `Unexpected removed resource: ${r.type}`,
    );
  assert.equal(ofType("aws:ec2/natGateway:NatGateway").length, 2);
  assert.equal(ofType("aws:ec2/subnet:Subnet").length, 6);
  const db = ofType("aws:rds/instance:Instance")[0].inputs;
  assert.equal(db.multiAz, true);
  assert.equal(db.publiclyAccessible, false);
  assert.equal(db.storageEncrypted, true);
  assert.equal(db.deletionProtection, true);
  const listener = ofType("aws:lb/listener:Listener")[0].inputs;
  assert.equal(listener.port, 993);
  assert.equal(listener.protocol, "TLS");
  const bucket = ofType("aws:s3/bucket:Bucket").find(
    (r) => r.name === "imap-artifacts",
  );
  assert(bucket);
  const block = ofType(
    "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock",
  ).find((r) => r.name === "imap-artifacts").inputs;
  assert.equal(block.blockPublicPolicy, true);
  assert.equal(block.blockPublicAcls, true);
  const launch = ofType("aws:ec2/launchTemplate:LaunchTemplate")[0].inputs;
  assert.equal(launch.instanceType, scaled ? "t3.medium" : "t3.small");
  assert.equal(launch.metadataOptions.httpTokens, "required");
  assert.equal(launch.blockDeviceMappings[0].ebs.encrypted, "true");
  const script = Buffer.from(launch.userData, "base64").toString();
  assert(script.includes("systemd") || script.includes("systemctl"));
  assert(script.includes("docker"));
  assert(!script.includes("synthetic-db-password-123"));
  assert(Buffer.byteLength(script) <= 16384);
  const envSecret = ofType("aws:s3/bucketObjectv2:BucketObjectv2").find(
    (r) => r.name === "imap-environment",
  ).inputs.content;
  const env = typeof envSecret === "string" ? envSecret : envSecret.value;
  assert(env.includes("IMAP_STORAGE_BACKEND=postgres"));
  assert(env.includes("IMAP_DB_SSLMODE=verify-full"));
  assert(env.includes(`IMAP_MAX_CONNECTIONS=${scaled ? 250 : 100}`));
  assert(!env.includes("IMAP_TRIAL_EXPIRES_AT"));
  const group = ofType("aws:autoscaling/group:Group")[0].inputs;
  assert.equal(group.desiredCapacity, enabled ? (scaled ? 3 : 2) : 0);
  assert.equal(group.minSize, enabled ? 2 : 0);
  assert.equal(group.maxSize, enabled ? (scaled ? 6 : 8) : 0);
  assert.equal(group.vpcZoneIdentifiers.length, 2);
  assert.deepEqual(ignored, enabled ? ["desiredCapacity"] : []);
  const policies = ofType("aws:autoscaling/policy:Policy");
  assert.equal(policies.length, enabled ? 1 : 0);
  if (enabled) {
    assert.equal(
      policies[0].inputs.targetTrackingConfiguration.targetValue,
      scaled ? 55 : 60,
    );
    assert.equal(
      policies[0].inputs.targetTrackingConfiguration
        .predefinedMetricSpecification.predefinedMetricType,
      "ASGAverageCPUUtilization",
    );
  }
  console.log(`Offline infrastructure checks passed (${mode}).`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
