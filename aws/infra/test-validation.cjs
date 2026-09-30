const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
for (const [mode, message] of [
  ["invalid-counts", "minInstances <= desiredInstances <= maxInstances"],
  ["invalid-target", "cpuTarget must be between"],
  ["invalid-instance", "Invalid instanceType"],
  ["invalid-connections", "maxConnections must be a positive integer"],
  ["invalid-nat", "natGateways must be 1 or 2"],
]) {
  const result = spawnSync(
    process.execPath,
    [path.join(__dirname, "test-infra.cjs"), mode],
    { encoding: "utf8" },
  );
  assert.notEqual(result.status, 0, `${mode} should fail`);
  assert(
    (result.stdout + result.stderr).includes(message),
    `${mode} should report configuration problem: ${result.stderr}`,
  );
}
console.log(
  "Invalid EC2 scaling configurations rejected before creating mocked resources.",
);
