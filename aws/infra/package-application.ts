import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

/** Package only public application files; private settings/state never enter S3. */
export function packageApplication() {
  const root = path.resolve(__dirname, "..");
  const entries = [
    "Dockerfile",
    "pyproject.toml",
    "agentmail_imap",
    "scripts/ensure_database.py",
    "certs/global-bundle.pem",
    "requirements.lock",
  ];
  const files: string[] = [];
  function visit(relative: string) {
    const full = path.join(root, relative);
    const stat = fs.lstatSync(full);
    if (stat.isSymbolicLink())
      throw new Error(`Artifact symlink refused: ${relative}`);
    if (stat.isDirectory()) {
      for (const name of fs.readdirSync(full).sort()) {
        if (name === "__pycache__" || name.endsWith(".pyc")) continue;
        visit(path.join(relative, name));
      }
    } else if (stat.isFile()) files.push(relative);
  }
  entries.forEach(visit);
  const hash = createHash("sha256");
  for (const file of files)
    hash
      .update(file)
      .update("\0")
      .update(fs.readFileSync(path.join(root, file)))
      .update("\0");
  const sourceHash = hash.digest("hex");
  const directory = path.join(root, ".generated");
  fs.mkdirSync(directory, { recursive: true });
  const imageTag = `agentmail-imap:${sourceHash}`;
  const archive = path.join(directory, `${sourceHash}.docker.tar.gz`);
  if (!fs.existsSync(archive)) {
    const context = fs.mkdtempSync(path.join(directory, "context-"));
    const tarPath = path.join(directory, `${sourceHash}.docker.tar`);
    try {
      for (const file of files) {
        const destination = path.join(context, file);
        fs.mkdirSync(path.dirname(destination), { recursive: true });
        fs.copyFileSync(path.join(root, file), destination);
      }
      execFileSync(
        "docker",
        ["build", "--platform", "linux/amd64", "-t", imageTag, context],
        { stdio: "inherit" },
      );
      execFileSync("docker", ["save", "--output", tarPath, imageTag], {
        stdio: "inherit",
      });
      execFileSync("gzip", ["-n", "-f", tarPath]);
      fs.renameSync(`${tarPath}.gz`, archive);
    } finally {
      fs.rmSync(context, { recursive: true, force: true });
      fs.rmSync(tarPath, { force: true });
    }
  }
  const sha256 = createHash("sha256")
    .update(fs.readFileSync(archive))
    .digest("hex");
  return {
    path: archive,
    sha256,
    key: `releases/${sha256}.docker.tar.gz`,
    imageTag,
  };
}
