"""Prepare the pinned ZCode CLI from upstream for a local SeekGnomes build."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "harness-assets/zcode-source-build"
WORK = ROOT / ".seekgnomes-zcode-build"
COMMIT = "fec975905a2abd4c07e35c40d51fa097a2258ec9"
NODE_VERSION = "24.14.0"
PNPM_VERSION = "10.33.2"
CLI_VERSION = "0.17.1"
NODE_NAME = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
SOURCE_URL = f"https://codeload.github.com/zai-org/ZCode/tar.gz/{COMMIT}"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_NAME}"
PNPM_URL = f"https://registry.npmjs.org/pnpm/-/pnpm-{PNPM_VERSION}.tgz"
SOURCE_SHA = "cde82985a3b68e9f4aba1491e65c64a4ef5893e1d0160793d3dfb7f4969cd86d"
NODE_SHA = "41cd79bb7877c81605a9e68ec4c91547774f46a40c67a17e34d7179ef11729df"
PNPM_SHA = "7a7bcf13d7f6ceb3946c03978373d99be9fde1cafc3000bdfed4c4f791167610"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, path, expected):
    if not path.is_file():
        partial = path.with_name(path.name + ".partial")
        with urlopen(url, timeout=180) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(partial) != expected:
            raise ValueError("Upstream SHA256 mismatch: " + path.name)
        partial.replace(path)
    if digest(path) != expected:
        raise ValueError("Local SHA256 mismatch: " + path.name)


def extract(path, directory, expected_root):
    if expected_root.is_dir():
        return
    with tarfile.open(path) as archive:
        archive.extractall(directory, filter="data")


def main():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("Run the Linux x86-64 bootstrap from Linux or WSL.")
    if not (ASSETS.parent / "manifest.json").is_file():
        raise SystemExit("Run build-harness-assets.py before building ZCode.")
    for directory in (WORK, ASSETS, ASSETS.parent):
        if directory.resolve() != directory:
            raise ValueError("Aliased build directory: " + str(directory))
        directory.mkdir(parents=True, exist_ok=True)
    output = ASSETS / f"zcode-{CLI_VERSION}-linux-x64-runtime.tar.gz"
    provenance = ASSETS / "build-provenance.json"
    node_archive = ASSETS / NODE_NAME
    if provenance.is_file():
        value = json.loads(provenance.read_text())
        if (value["source_commit"] != COMMIT or digest(output) != value["runtime_archive_sha256"]
                or digest(node_archive) != NODE_SHA):
            raise ValueError("Existing ZCode assets do not match their build record")
        return

    source_archive = WORK / "zcode-source.tar.gz"
    pnpm_archive = WORK / f"pnpm-{PNPM_VERSION}.tgz"
    fetch(SOURCE_URL, source_archive, SOURCE_SHA)
    fetch(NODE_URL, node_archive, NODE_SHA)
    fetch(PNPM_URL, pnpm_archive, PNPM_SHA)
    source = WORK / ("ZCode-" + COMMIT)
    node_root = WORK / f"node-v{NODE_VERSION}-linux-x64"
    pnpm_root = WORK / "package"
    extract(source_archive, WORK, source)
    extract(node_archive, WORK, node_root)
    extract(pnpm_archive, WORK, pnpm_root)
    home = WORK / "home"
    home.mkdir(exist_ok=True)
    environment = {
        "PATH": str(node_root / "bin") + ":/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "npm_config_ignore_scripts": "true",
        "CI": "true",
        "LANG": "C.UTF-8",
    }
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if key in os.environ:
            environment[key] = os.environ[key]
    node = node_root / "bin/node"
    pnpm = [str(node), str(pnpm_root / "bin/pnpm.cjs")]

    def run(*args):
        subprocess.run([*pnpm, *args], cwd=source, env=environment, check=True)

    run("--filter", "@zcode/cli...", "install", "--frozen-lockfile", "--ignore-scripts")
    run("exec", "tsc", "-b", "packages/shared", "--force")
    run("--filter", "@zcode/cli...", "--filter", "!@zcode/cli",
        "--workspace-concurrency=3", "-r", "run", "build")
    run("--filter", "@zcode/cli", "build")
    deployed = WORK / "deployed"
    if deployed.exists():
        if deployed.resolve() != deployed:
            raise ValueError("Aliased ZCode deployment directory")
        shutil.rmtree(deployed)
    run("--config.allow-unused-patches=true", "--filter", "@zcode/cli",
        "deploy", "--legacy", "--prod", str(deployed))

    entry = deployed / "dist/zcode.cjs"
    probes = {}
    for probe in ("version", "help"):
        result = subprocess.run([str(node), str(entry), "--" + probe], cwd=deployed,
                                env=environment, capture_output=True, text=True, timeout=60, check=True)
        if probe == "version" and result.stdout.strip() != CLI_VERSION:
            raise ValueError("Unexpected ZCode CLI version")
        probes[probe + "_probe"] = {"exit_code": result.returncode, "stdout": result.stdout[:4096]}
    partial = output.with_name(output.name + ".partial")
    with tarfile.open(partial, "w:gz", compresslevel=1) as archive:
        for name in ("dist", "node_modules", "package.json"):
            archive.add(deployed / name, arcname=name)
    partial.replace(output)
    shutil.copyfile(source / "LICENSE", ASSETS / "SOURCE_LICENSE.txt")
    shutil.copyfile(source / "pnpm-lock.yaml", ASSETS / "upstream-pnpm-lock.yaml")
    record = {
        "source_url": SOURCE_URL, "source_commit": COMMIT, "source_archive_sha256": SOURCE_SHA,
        "pnpm_version": PNPM_VERSION, "pnpm_tarball": PNPM_URL, "pnpm_archive_sha256": PNPM_SHA,
        "node_version": NODE_VERSION, "node_url": NODE_URL, "node_sha256": NODE_SHA,
        "cli_version": CLI_VERSION, "cli_sha256": digest(entry), "runtime_archive_sha256": digest(output),
        "upstream_lock_sha256": digest(source / "pnpm-lock.yaml"),
        "build_dependency_scripts_ignored": True, "external_model_api_calls": 0, **probes,
    }
    temporary = provenance.with_suffix(".partial")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(provenance)


if __name__ == "__main__":
    main()
