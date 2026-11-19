"""Fetch pinned official native CLI assets into this release, never globally.

Run with Python 3.11+ on Linux x86-64. npm lifecycle scripts are disabled;
installed binaries are checked with --version before the asset is sealed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

RELEASE = Path(__file__).resolve().parent
PROJECT = RELEASE.parents[1]
if not (PROJECT / "revisions/seekgnomes_v1_2/harnesses.py").is_file():
    PROJECT = RELEASE / "payload/Projects/QuantumManyBodyVariational"
if not (PROJECT / "revisions/seekgnomes_v1_2/harnesses.py").is_file():
    raise RuntimeError("Pinned harness registry is missing from the source payload")
sys.path.insert(0, str(PROJECT))
from revisions.seekgnomes_v1_2.harnesses import installation_manifest

NODE_VERSION = "24.9.0"
NODE_NAME = "node-v" + NODE_VERSION + "-linux-x64.tar.xz"
# SHA-256 recorded on 2026-11-17 from Xiaomi's official versioned HTTPS
# release. Its optional upstream .sha256 endpoint returned HTTP 404.
MIMO_BASELINE_SHA256 = "92b0d0e5e7cc93691a072aa34a6496d01799e84ae0f13f369cdc14a07a35d66a"
NODE_SHA256 = "f52ec50e959d72d5c680d9731420b2661cd2a8070e94c7369b6ddfcd8b7278be"
NPM_LOCK_SHA256 = "6add1b2cdd50b0a637154699212bbc68085a1812a3aaceb25e6c62f9017e68c1"
WORK = RELEASE / ".seekgnomes-native-build"
ASSETS = RELEASE / "harness-assets"


def digest(path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(url, path):
    if path.is_file():
        return
    temporary = path.with_name(path.name + ".partial")
    with urllib.request.urlopen(url, timeout=180) as response, temporary.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    temporary.replace(path)


def main():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("Run this pinned Linux x86-64 bootstrap from Linux or WSL.")
    if (ASSETS / "manifest.json").is_file():
        subprocess.run([sys.executable, str(RELEASE / "verify-harness-assets.py"), str(ASSETS)], check=True)
        return
    for directory in (WORK, ASSETS):
        if directory.exists() and directory.resolve() != directory:
            raise ValueError("Asset directory is an alias")
        directory.mkdir(parents=True, exist_ok=True)
    node_archive = ASSETS / NODE_NAME
    download("https://nodejs.org/dist/v" + NODE_VERSION + "/" + NODE_NAME, node_archive)
    if digest(node_archive) != NODE_SHA256:
        raise ValueError("Pinned Node archive SHA256 mismatch")
    node_root = WORK / ("node-v" + NODE_VERSION + "-linux-x64")
    if not node_root.exists():
        with tarfile.open(node_archive) as archive:
            archive.extractall(WORK, filter="data")
    node = node_root / "bin/node"
    npm = node_root / "lib/node_modules/npm/bin/npm-cli.js"
    stage = WORK / "npm-tree"
    stage.mkdir(exist_ok=True)
    manifests = installation_manifest()
    dependencies = {item["package"]: item["version"] for item in manifests.values() if item.get("kind") == "npm"}
    lock = RELEASE / "native-package-lock.json"
    if digest(lock) != NPM_LOCK_SHA256:
        raise ValueError("Frozen npm dependency lock SHA256 mismatch")
    package = json.loads(lock.read_text())["packages"][""]
    if package["dependencies"] != dependencies:
        raise ValueError("Frozen npm lock does not match the harness registry")
    package["private"] = True
    (stage / "package.json").write_text(json.dumps(package, indent=2) + "\n")
    shutil.copyfile(lock, stage / "package-lock.json")
    environment = {"PATH": str(node_root / "bin") + ":/usr/local/bin:/usr/bin:/bin",
        "HOME": str(WORK / "home"), "TMPDIR": str(WORK / "tmp"),
        "XDG_CACHE_HOME": str(WORK / "cache"), "npm_config_cache": str(WORK / "npm-cache"),
        "npm_config_update_notifier": "false", "npm_config_fund": "false",
        "npm_config_audit": "false", "LANG": "C.UTF-8"}
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if key in os.environ:
            environment[key] = os.environ[key]
    for name in ("home", "tmp", "cache"):
        (WORK / name).mkdir(exist_ok=True)
    command = [str(node), str(npm), "ci", "--prefix", str(stage), "--ignore-scripts",
               "--no-audit", "--no-fund", "--omit=dev"]
    print(json.dumps({"stage": "npm_install", "package_versions": dependencies}), flush=True)
    subprocess.run(command, env=environment, check=True)
    lock = json.loads((stage / "package-lock.json").read_text())
    for item in manifests.values():
        if item.get("kind") != "npm":
            continue
        record = lock["packages"]["node_modules/" + item["package"]]
        if record["version"] != item["version"] or record.get("integrity") != item["integrity"]:
            raise ValueError("Resolved top-level harness differs from pinned official asset")
    # Claude's published wrapper has a placeholder until its lifecycle script
    # selects an optional native package. Select the same pinned Linux/glibc
    # asset locally, without executing downloaded npm lifecycle scripts.
    claude_package = stage / "node_modules/@anthropic-ai/claude-code-linux-x64"
    claude_record = json.loads((claude_package / "package.json").read_text())
    claude_version = manifests["claude-code"]["version"]
    if claude_record.get("version") != claude_version:
        raise ValueError("Claude native platform asset has an unexpected version")
    claude_binary = claude_package / "claude"
    if not claude_binary.is_file() or claude_binary.open("rb").read(4) != b"\x7fELF":
        raise ValueError("Claude native platform asset is not a Linux executable")
    claude_entry = stage / "node_modules/.bin/claude"
    claude_entry.unlink(missing_ok=True)
    claude_entry.symlink_to("../@anthropic-ai/claude-code-linux-x64/claude")
    # MiMo's npm entry point otherwise downloads a CPU-selected executable on
    # first invocation. Prefetch its official baseline build explicitly so the
    # image works without AVX2 and never downloads executable code at runtime.
    mimo_version = manifests["mimo-code"]["version"]
    mimo_name = "mimocode-linux-x64-baseline.tar.gz"
    mimo_url = "https://mimocode.cnbj1.mi-fds.com/mimocode/mimocode/releases/v" + mimo_version + "/" + mimo_name
    mimo_archive = ASSETS / mimo_name
    download(mimo_url, mimo_archive)
    expected_mimo = MIMO_BASELINE_SHA256
    if digest(mimo_archive) != expected_mimo:
        raise ValueError("Pinned MiMo baseline archive SHA256 mismatch")
    mimo_unpack = WORK / "mimo-baseline"
    mimo_unpack.mkdir(exist_ok=True)
    with tarfile.open(mimo_archive) as archive:
        archive.extractall(mimo_unpack, filter="data")
    candidates = [path for path in mimo_unpack.rglob("mimo") if path.is_file()]
    if len(candidates) != 1:
        raise ValueError("MiMo baseline archive has an ambiguous executable")
    mimo_binary = stage / "node_modules/@mimo-ai/cli/vendor/mimo"
    mimo_binary.parent.mkdir(exist_ok=True)
    shutil.copyfile(candidates[0], mimo_binary)
    mimo_binary.chmod(0o755)
    probes = {}
    for name, item in manifests.items():
        if item.get("kind") != "npm":
            continue
        binary = stage / "node_modules/.bin" / item["binary"]
        try:
            result = subprocess.run([str(binary), "--version"], env=environment, capture_output=True,
                                    text=True, timeout=60)
            output = (result.stdout + result.stderr).strip()
            ok = result.returncode == 0 and re.search(r"(?<![0-9.])" + re.escape(item["version"]) + r"(?![0-9.])", output) is not None
            probes[name] = {"ok": ok, "expected_version": item["version"], "returncode": result.returncode,
                            "output": output[:4000]}
        except (OSError, subprocess.TimeoutExpired) as error:
            probes[name] = {"ok": False, "expected_version": item["version"],
                            "returncode": None, "output": str(error)[:4000]}
        print(json.dumps({"stage": "version_probe", "harness": name, **probes[name]}), flush=True)
    if not all(item["ok"] for item in probes.values()):
        (WORK / "failed-version-probes.json").write_text(json.dumps(probes, indent=2) + "\n")
        raise ValueError("Native CLI version probes failed; refusing to seal ready assets")
    tree_archive = ASSETS / "native-node-modules.tar.gz"
    if tree_archive.exists():
        raise FileExistsError("Native asset archive already exists; use a new versioned release instead of overwriting")
    with tarfile.open(tree_archive, "w:gz", compresslevel=1) as archive:
        archive.add(stage / "node_modules", arcname="node_modules")
        archive.add(stage / "package.json", arcname="package.json")
        archive.add(stage / "package-lock.json", arcname="package-lock.json")
    shutil.copyfile(stage / "package-lock.json", ASSETS / "native-package-lock.json")
    install = ASSETS / "install.sh"
    install.write_text("""#!/usr/bin/env bash
set -euo pipefail
assets="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
target=/opt/seekgnomes/harness
mkdir -p "$target/node" "$target/bin"
tar -xJf "$assets/""" + NODE_NAME + """" -C "$target/node" --strip-components=1
tar -xzf "$assets/native-node-modules.tar.gz" -C "$target"
ln -s ../node/bin/node "$target/bin/node"
ln -s ../node/bin/npm "$target/bin/npm"
for executable in "$target"/node_modules/.bin/*; do
    name="$(basename -- "$executable")"
    test "$name" = node && continue
    test "$name" = npm && continue
    ln -s "../node_modules/.bin/$name" "$target/bin/$name"
done
""")
    install.chmod(0o755)
    files = {path.name: digest(path) for path in sorted(ASSETS.iterdir()) if path.is_file() and path.name != "manifest.json"}
    record = {"schema": "seekgnomes-native-assets/v1.2", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": "linux-amd64", "node_version": NODE_VERSION, "files": files,
        "top_level_official_assets": manifests, "version_probes": probes,
        "network_during_image_install": False, "npm_lifecycle_scripts_executed": False,
        "manual_platform_selection": {"claude-code": "@anthropic-ai/claude-code-linux-x64@" + claude_version,
            "mimo-code": {"target": "mimocode-linux-x64-baseline", "version": mimo_version,
                          "source_url": mimo_url, "archive_sha256": expected_mimo,
                          "upstream_checksum_available": False,
                          "checksum_origin": "recorded from official HTTPS asset; optional upstream checksum HTTP404",
                          "executable_sha256": digest(mimo_binary)}},
        "not_bundled": {name: item for name, item in manifests.items() if item.get("kind") != "npm"}}
    (ASSETS / "manifest.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"stage": "ready", "asset_dir": str(ASSETS), "bytes": sum(p.stat().st_size for p in ASSETS.iterdir() if p.is_file())}), flush=True)


if __name__ == "__main__":
    main()
