const { spawnSync } = require("node:child_process");
const path = require("node:path");

for (const [command, ...prefix] of [["python3"], ["python"], ["py", "-3.12"]]) {
  const probe = spawnSync(command, [...prefix, "-c",
    "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)"], { stdio: "ignore" });
  if (probe.error || probe.status !== 0) continue;

  const result = spawnSync(command, [...prefix, path.join(__dirname, "prepare_local_stack.py"),
    ...process.argv.slice(2)], { stdio: "inherit" });
  if (result.error || result.signal) {
    console.error("Your Python build process could not complete. Check your Python installation, then retry your build.");
  }
  process.exit(result.status ?? 1);
}

const check = process.platform === "win32" ? ".\\start.ps1 --check" : "./start --check";
console.error(`Your build needs Python 3.12. Run ${check} for your install steps, then retry your build.`);
process.exit(1);
