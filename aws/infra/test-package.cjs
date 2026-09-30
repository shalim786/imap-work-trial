// Verify allowlisted image packaging without starting Docker or AWS.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const child = require("node:child_process");
const ts = require("typescript");
const original = child.execFileSync;
const root = path.resolve(__dirname, "..");
const generated = path.join(root, ".generated");
const before = new Set(
  fs.existsSync(generated) ? fs.readdirSync(generated) : [],
);
let inspected = false;
child.execFileSync = (program, args, options) => {
  if (program !== "docker") return original(program, args, options);
  if (args[0] === "build") {
    const context = args.at(-1);
    assert.deepEqual(fs.readdirSync(context).sort(), [
      "Dockerfile",
      "agentmail_imap",
      "certs",
      "pyproject.toml",
      "requirements.lock",
      "scripts",
    ]);
    assert.deepEqual(fs.readdirSync(path.join(context, "scripts")), [
      "ensure_database.py",
    ]);
    assert(!fs.existsSync(path.join(context, "agentmail_imap", "__pycache__")));
    assert(args.includes("linux/amd64"));
    inspected = true;
  } else if (args[0] === "save")
    fs.writeFileSync(args[2], "synthetic Docker archive");
  else throw new Error("Unexpected Docker operation");
};
require.extensions[".ts"] = (mod, filename) =>
  mod._compile(
    ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText,
    filename,
  );
try {
  const { packageApplication } = require("./package-application.ts");
  const artifact = packageApplication();
  assert(/^[a-f0-9]{64}$/.test(artifact.sha256));
  assert.equal(artifact.key, `releases/${artifact.sha256}.docker.tar.gz`);
  assert(artifact.imageTag.startsWith("agentmail-imap:"));
  assert.deepEqual(packageApplication(), artifact);
  assert(inspected || before.has(path.basename(artifact.path)));
  const { buildUserData } = require("./ec2-bootstrap.ts");
  const script = buildUserData({
    region: "us-east-1",
    bucket: "private-test",
    key: artifact.key,
    sha256: artifact.sha256,
    imageTag: artifact.imageTag,
    environmentKey: "configuration/runtime.env",
  });
  original("bash", ["-n"], { input: script });
  for (const text of ["docker load", "ensure_database.py", "chmod 600"])
    assert(script.includes(text));
  assert(
    !/secretsmanager|cloudwatch|IMAP_DB_PASSWORD=|TRIAL_EXPIRES/.test(script),
  );
  console.log("Docker archive allowlist, reuse, and EC2 shell checks passed.");
} finally {
  child.execFileSync = original;
  for (const name of fs.readdirSync(generated))
    if (!before.has(name))
      fs.rmSync(path.join(generated, name), { recursive: true, force: true });
}
