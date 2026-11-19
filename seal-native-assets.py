"""Merge the pinned ZCode source build into the verified native asset bundle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RELEASE = Path(__file__).resolve().parent
ASSETS = RELEASE / "harness-assets"
ZCODE = ASSETS / "zcode-source-build"
MARKER = "# ZCode pinned source build\n"


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    record = json.loads((ASSETS / "manifest.json").read_text())
    provenance = json.loads((ZCODE / "build-provenance.json").read_text())
    if provenance["source_commit"] != "fec975905a2abd4c07e35c40d51fa097a2258ec9":
        raise ValueError("Unexpected ZCode source revision")
    if provenance["cli_version"] != "0.17.1" or provenance["node_version"] != "24.14.0":
        raise ValueError("Unexpected ZCode CLI or Node version")
    for name, field in (("zcode-0.17.1-linux-x64-runtime.tar.gz", "runtime_archive_sha256"),
                        ("node-v24.14.0-linux-x64.tar.xz", "node_sha256")):
        path = ZCODE / name
        if path.resolve(strict=True) != path or sha(path) != provenance[field]:
            raise ValueError("ZCode build asset differs from provenance: " + name)
    if provenance["version_probe"].get("exit_code") != 0 or provenance["help_probe"].get("exit_code") != 0:
        raise ValueError("ZCode source build did not pass its native CLI probes")
    # Refuse changes to previously sealed assets, except this intentional
    # installer extension and newly added source-build evidence.
    for name, expected in record["files"].items():
        path = ASSETS / name
        if name != "install.sh" and not name.startswith("zcode-source-build/"):
            if path.resolve(strict=True) != path or sha(path) != expected:
                raise ValueError("A previously sealed native asset changed: " + name)
    install = ASSETS / "install.sh"
    base = install.read_text().split(MARKER, 1)[0]
    install.write_text(base + MARKER + r'''mkdir -p "$target/zcode/node"
tar -xzf "$assets/zcode-source-build/zcode-0.17.1-linux-x64-runtime.tar.gz" -C "$target/zcode"
tar -xJf "$assets/zcode-source-build/node-v24.14.0-linux-x64.tar.xz" -C "$target/zcode/node" --strip-components=1
cat > "$target/bin/zcode" <<'ZCODE_ENTRY'
#!/usr/bin/env sh
wrapper_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
exec "$wrapper_dir/../zcode/node/bin/node" "$wrapper_dir/../zcode/dist/zcode.cjs" "$@"
ZCODE_ENTRY
chmod 755 "$target/bin/zcode"
''')
    install.chmod(0o755)
    record["source_builds"] = {"zcode": provenance}
    registry_path = RELEASE.parents[1] / "revisions/seekgnomes_v1_2/harness_registry.json"
    if not registry_path.is_file():
        registry_path = RELEASE / "payload/Projects/QuantumManyBodyVariational/revisions/seekgnomes_v1_2/harness_registry.json"
    registry = json.loads(registry_path.read_text())
    record["top_level_official_assets"] = {
        name: {"binary": row["binary"], **row["install"]}
        for name, row in registry["harnesses"].items()
    }
    record.setdefault("version_probes", {})["zcode"] = {
        "ok": True, "expected_version": provenance["cli_version"],
        "returncode": provenance["version_probe"]["exit_code"],
        "output": provenance["version_probe"].get("stdout", ""),
        "scope": "native CLI version and help; model/MCP acceptance is separate",
    }
    record.setdefault("not_bundled", {}).pop("zcode", None)
    files = {}
    for path in sorted(ASSETS.rglob("*")):
        if not path.is_file() or path == ASSETS / "manifest.json":
            continue
        if path.resolve() != path:
            raise ValueError("Aliased native source-build asset")
        files[str(path.relative_to(ASSETS))] = sha(path)
    record["files"] = files
    (ASSETS / "manifest.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "harnesses": sorted(record["version_probes"]),
                      "asset_files": len(files)}))


if __name__ == "__main__":
    main()
