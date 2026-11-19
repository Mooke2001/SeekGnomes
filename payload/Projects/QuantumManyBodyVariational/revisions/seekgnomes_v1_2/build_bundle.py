"""Build and seal the allowlisted Harbor release without reading credentials.

Large runtime components remain separate from the GitHub source archive. This
module only packages existing assets; it never installs a CLI or contacts an API.
"""
from __future__ import annotations

import argparse
import ast
import configparser
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
from tempfile import TemporaryDirectory
import zipfile

PROJECT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VERSION = "1.3.0"
DIST = f"seekgnomes-v{VERSION}"
RELEASE = PROJECT / "releases" / DIST
PREVIOUS = PROJECT / "releases/seekgnomes-v1.2.0"
ARTIFACT_PART_BYTES = 1_900_000_000
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".sh"}
STATE_PROOF_LOCK = "revisions/seekgnomes_v1_2/STATE_PROOF_SPEC.lock.json"
CONSOLE = PROJECT / "revisions/seekgnomes_console_v1"
CONSOLE_FILES = (
    "README.md", "SHARING.md", "Start.command", "Start WSL.cmd", "build_console.py",
    "receiver.py", "runner.py", "server.py", "sharing.py",
    "runtime/parallel_campaign.py", "runtime/runtime_entry.py",
    "test_budget_profile.py", "test_console_server.py", "test_parallel_campaign.py", "test_sharing.py",
    "ui/app.js", "ui/index.html", "ui/style.css",
)
TEMPLATE_FILES = (
    "Dockerfile", "build-image.sh", "verify-harness-assets.py", "build-harness-assets.py",
    "seal-native-assets.py", ".dockerignore", ".gitignore", "seekgnomes", "seekgnomes.ps1",
    "Start.command", "THIRD_PARTY_NOTICES.md", "requirements-science.lock",
    "requirements-harbor.lock", "harbor-wheel-manifest.json", "restore-artifact.py",
    "restore-artifact.ps1",
)
ASSET_DIRS = ("runtime", "mathlib-packages", "science-wheels", "harbor-wheels", "harness-assets")
PUBLIC_ASSET_DIRS = tuple(name for name in ASSET_DIRS if name != "harness-assets")
PROJECT_PAYLOAD = Path("payload/Projects/QuantumManyBodyVariational")


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_closure():
    names = {
        "STATE_SPEC_V1.lock.json", "STATE_SPEC_V2.lock.json", "HYBRID_PROOF_SPEC.lock.json",
        "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json", "config/benchmark50_tasks.json",
        "config/benchmark50_score_rules.json", "PYTHON_CORRELATOR_HYBRID.md", "STATE_FORMAT.md",
        "INFINITE_STATE_LEAN.md", "STATE_SPEC_V1.md", "STATE_SPEC_V2.md",
        STATE_PROOF_LOCK, "scripts/state_format.py", "scripts/check_state_format_lean.py",
    }
    names.update(str(p.relative_to(PROJECT)) for p in (PROJECT / "examples/state_format").iterdir()
                 if p.is_file() and p.suffix in {".state", ".lean"})
    names.update(str(p.relative_to(PROJECT)) for p in HERE.rglob("*")
                 if p.is_file() and p.suffix in TEXT_SUFFIXES and "__pycache__" not in p.parts)
    # Shared score/reference records are unchanged and explicitly included.
    for name in ("SCORING.md", "task_groups.json", "exact_reference_catalogue.json"):
        names.add("revisions/seekgnomes_v1_1_1/" + name)
    # These are runtime data read through Path expressions, rather than Python
    # imports; static AST dependency discovery cannot discover them.
    for name in ("constructor.md", "formalist.md"):
        names.add("revisions/seekgnomes_v1_1_1/prompts/" + name)
    done = set()

    def include_module(module):
        base = module.replace(".", "/")
        for candidate in (base + ".py", base + "/__init__.py"):
            if (PROJECT / candidate).is_file():
                names.add(candidate)
        parent = Path(base).parent
        while parent != Path("."):
            init = str(parent / "__init__.py")
            if (PROJECT / init).is_file():
                names.add(init)
            parent = parent.parent

    while pending := names - done:
        for name in sorted(pending):
            path = PROJECT / name
            if not path.is_file() or path.resolve() != path:
                raise ValueError("Missing or aliased source: " + name)
            done.add(name)
            if path.suffix == ".json":
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    for item, expected in data.get("source_sha256", {}).items():
                        if not isinstance(expected, str):
                            continue
                        locked = PROJECT / item
                        if locked.resolve() != locked or sha(locked) != expected:
                            raise ValueError("Frozen source changed: " + item)
                        names.add(item)
            if path.suffix != ".py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        include_module(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        parts = list(Path(name).parent.parts)
                        module = ".".join(parts[:len(parts) - node.level + 1] + ([module] if module else []))
                    include_module(module)
                    for alias in node.names:
                        if alias.name != "*":
                            include_module(module + "." + alias.name)
    return sorted(names)


def _checked_tree(source):
    source = Path(source)
    if source.resolve(strict=True) != source:
        raise ValueError("Top-level component is an alias: " + str(source))
    for item in source.rglob("*"):
        if item.is_symlink() and not item.resolve().is_relative_to(source):
            raise ValueError("External symbolic link in runtime: " + str(item))
    return source


def copytree_exact(source, destination):
    source, destination = _checked_tree(source), Path(destination)
    if destination.exists():
        if destination.resolve() != destination:
            raise ValueError("Aliased component destination")
        return
    # Frozen verifier assets require single-link regular files. Always create
    # independent inodes, including when source and destination share a disk.
    with TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        shutil.copytree(source, staged, symlinks=True)
        staged.rename(destination)


def wheel_lock(directory, output):
    """Pin every local wheel by METADATA identity and SHA-256."""
    directory = Path(directory)
    records = []
    for path in sorted(directory.glob("*.whl")):
        if path.resolve() != path:
            raise ValueError("Wheel archive is an alias")
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(candidates) != 1:
                raise ValueError("Wheel has ambiguous package metadata")
            metadata = BytesParser().parsebytes(archive.read(candidates[0]))
        records.append({"name": metadata["Name"], "version": metadata["Version"],
                        "filename": path.name, "sha256": sha(path), "bytes": path.stat().st_size})
    normalized = [record["name"].lower().replace("_", "-") for record in records]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Multiple wheels for one package require explicit platform lock separation")
    if not any(r["name"].lower() == "harbor" and r["version"] == "0.22.0" for r in records):
        raise ValueError("Harbor 0.22.0 wheel is missing")
    Path(output).write_text("# Linux amd64, CPython 3.12; generated from this release's wheelhouse.\n" + "".join(
        f'{r["name"]}=={r["version"]} --hash=sha256:{r["sha256"]}\n' for r in records))
    return records


def verify_harness_assets(directory):
    directory = _checked_tree(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or "install.sh" not in files:
        raise ValueError("Native harness assets need a hashed install.sh and files map")
    for name, expected in files.items():
        relative = Path(name)
        path = directory / relative
        if relative.is_absolute() or ".." in relative.parts or path.resolve() != path:
            raise ValueError("Noncanonical harness asset")
        if not isinstance(expected, str) or sha(path) != expected:
            raise ValueError("Native harness asset hash mismatch: " + name)
    return manifest


def canonical_path(path):
    if path.resolve() != path or not path.is_relative_to(PROJECT):
        raise ValueError("Bundle paths must be unaliased and inside the project: " + str(path))
    return path


def copy_file(source, destination):
    canonical_path(source)
    canonical_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage_release_files(release):
    for name in TEMPLATE_FILES:
        local = HERE / name
        copy_file(local if local.is_file() else PREVIOUS / name, release / name)
    if (HERE / "build-zcode-assets.py").is_file():
        copy_file(HERE / "build-zcode-assets.py", release / "build-zcode-assets.py")
    copy_file(PREVIOUS / "harness-assets/native-package-lock.json", release / "native-package-lock.json")
    for name in ("seekgnomes", "seekgnomes.ps1", "build-image.sh"):
        path = release / name
        contents = path.read_text().replace("seekgnomes:1.2.0", f"seekgnomes:{VERSION}")
        path.write_text(contents)
    ignore = release / ".gitignore"
    rules = ignore.read_text().splitlines()
    # The dependency runtime lives at the root. A bare runtime/ rule would
    # also hide the checked-in console and benchmark runtime packages.
    ignore.write_text("\n".join("/" + rule if rule == "runtime/" else rule for rule in rules) + "\n")
    copy_file(HERE / "BUILD.md", release / "BUILD.md")
    for name in ("README.md", "PROTOCOL.md", "STATE_USAGE.md", "HARNESS_SOURCES.md", "harness_registry.json"):
        copy_file(HERE / name, release / name)
    copy_file(PROJECT / "STATE_FORMAT.md", release / "STATE_FORMAT.md")
    for name in ("SCORING.md", "task_groups.json", "exact_reference_catalogue.json"):
        copy_file(PROJECT / "revisions/seekgnomes_v1_1_1" / name, release / name)
    console = release / "console-src"
    for name in CONSOLE_FILES:
        copy_file(CONSOLE / name, console / name)


def build(include_assets=True, *, release=RELEASE):
    release = canonical_path(Path(release).absolute())
    if any(release.is_relative_to(path) for path in RELEASE.parent.glob("seekgnomes-v*")
           if path.is_dir() and path != RELEASE):
        raise ValueError("Older releases are sealed")
    release.mkdir(parents=True, exist_ok=True)
    payload = canonical_path(release / PROJECT_PAYLOAD)
    payload.mkdir(parents=True, exist_ok=True)
    names = source_closure()
    manifest_path = canonical_path(release / "source-manifest.json")
    if manifest_path.exists():
        for old in json.loads(manifest_path.read_text()).get("files", {}):
            relative = Path(old)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Noncanonical previous bundle entry")
            stale = payload / relative
            if old not in names and stale.is_file():
                if stale.resolve() != stale:
                    raise ValueError("Aliased stale payload source")
                stale.unlink()
    for name in names:
        copy_file(PROJECT / name, payload / name)
        (payload / name).chmod(0o644)
    stage_release_files(release)
    if include_assets:
        for name in ASSET_DIRS:
            copytree_exact(PREVIOUS / name, release / name)
        wheels = wheel_lock(release / "harbor-wheels", release / "requirements-harbor.lock")
        frozen_wheels = json.loads((PREVIOUS / "harbor-wheel-manifest.json").read_text())["packages"]
        if wheels != frozen_wheels:
            raise ValueError("The staged Harbor wheels differ from the sealed wheelhouse")
        native = verify_harness_assets(release / "harness-assets")
        if sha(release / "harness-assets/manifest.json") != sha(PREVIOUS / "harness-assets/manifest.json"):
            raise ValueError("The staged CLI manifest differs from the sealed manifest")
    else:
        frozen_assets = json.loads((PREVIOUS / "build-assets.json").read_text())
        wheels = frozen_assets["harbor_wheels"]
        native = frozen_assets["native_harnesses"]
    manifest = {"schema": "seekgnomes-source-bundle/v1.2", "version": VERSION,
                "container_project": "/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational",
                "files": {name: sha(payload / name) for name in names},
                "proof_lock_sha256": sha(payload / "revisions/dual_agent_v1_1/PROOF_SPEC.lock.json"),
                "state_proof_lock_sha256": sha(payload / STATE_PROOF_LOCK),
                "console_files": {name: sha(release / "console-src" / name) for name in CONSOLE_FILES},
                "contains_credentials": False, "contains_scientific_runs": False,
                "harbor_version": "0.22.0"}
    serialized = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(serialized)
    canonical_path(payload / "SEEKGNOMES_RELEASE.json").write_text(serialized)
    assets = {"schema": "seekgnomes-build-assets/v1.2", "assets_included": include_assets,
              "asset_base_version": "1.2.0",
              "harbor_wheels": wheels, "native_harnesses": native,
              "asset_dirs": list(ASSET_DIRS) if include_assets else [],
              "source_manifest_sha256": sha(manifest_path)}
    canonical_path(release / "build-assets.json").write_text(json.dumps(assets, indent=2, sort_keys=True) + "\n")
    return manifest


def split_artifact(path, *, part_bytes=ARTIFACT_PART_BYTES):
    path = Path(path)
    if not isinstance(part_bytes, int) or part_bytes <= 0:
        raise ValueError("A positive multipart size is required")
    if path.stat().st_size <= part_bytes:
        return None
    manifest_path = path.with_name(path.name + ".parts.json")
    if manifest_path.exists():
        raise FileExistsError("Multipart manifest already exists")
    record = {"schema": "seekgnomes-multipart/v1", "name": path.name,
              "bytes": path.stat().st_size, "parts": []}
    whole = hashlib.sha256()
    with path.open("rb") as source:
        index = 1
        while source.tell() < record["bytes"]:
            part_path = path.with_name(path.name + f".part{index:03d}")
            digest, count = hashlib.sha256(), 0
            with part_path.open("xb") as output:
                while count < part_bytes:
                    block = source.read(min(1024 * 1024, part_bytes - count))
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    whole.update(block)
                    count += len(block)
            record["parts"].append({"name": part_path.name, "bytes": count,
                                     "sha256": digest.hexdigest()})
            index += 1
    record["sha256"] = whole.hexdigest()
    if sum(part["bytes"] for part in record["parts"]) != record["bytes"]:
        raise ValueError("Archive changed size while creating release parts")
    manifest_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return {"manifest": manifest_path.name, "manifest_sha256": sha(manifest_path), **record}


def validated_image(release):
    validation = json.loads(canonical_path(release / "validation-summary.json").read_text())
    if validation.get("ok") is not True:
        raise ValueError("Successful image validation is required before release assembly")
    if validation.get("source_manifest_sha256") != sha(release / "source-manifest.json"):
        raise ValueError("Validation evidence does not match the current source manifest")
    image = canonical_path(release / "build-image-id.txt").read_text().strip()
    if not image or validation.get("image_id", "").removeprefix("sha256:") != image.removeprefix("sha256:"):
        raise ValueError("Validation evidence does not match the local image ID")
    return image


def sealed_files(release):
    manifest_path = canonical_path(release / "source-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if manifest["version"] != VERSION:
        raise ValueError("The staged source belongs to a different release")
    files = {PROJECT_PAYLOAD / name: expected for name, expected in manifest["files"].items()}
    files.update({Path("console-src") / name: expected for name, expected in manifest["console_files"].items()})
    files[PROJECT_PAYLOAD / "SEEKGNOMES_RELEASE.json"] = sha(manifest_path)
    for name, expected in files.items():
        if name.is_absolute() or ".." in name.parts or sha(canonical_path(release / name)) != expected:
            raise ValueError("Staged file differs from the release manifest: " + str(name))
    return list(files)


def archive_metadata(release, asset_dirs, image):
    assets = json.loads((release / "build-assets.json").read_text())
    manifest_hash = sha(release / "source-manifest.json")
    if assets["source_manifest_sha256"] != manifest_hash:
        raise ValueError("The build asset and source manifests disagree")
    assets.update({"assets_included": False, "asset_dirs": list(asset_dirs),
                   "vendor_runtime_acquisition": "on_the_build_host_from_pinned_upstream"})
    provenance = {"schema": "seekgnomes-build-provenance/v1.3", "version": VERSION,
                  "source_manifest_sha256": manifest_hash,
                  "independent_build_recipe": "Dockerfile",
                  "independent_build_recipe_sha256": sha(release / "Dockerfile"),
                  "bundled_asset_dirs": list(asset_dirs),
                  "bit_identical_independent_rebuild_claimed": False}
    if image is not None:
        provenance.update({"validated_local_image_id": image,
                           "validation_summary_sha256": sha(release / "validation-summary.json")})
    return {"build-assets.json": assets, "build-provenance.json": provenance}


def check_public_git_assets(release, assets_root):
    manifest_path = release / PROJECT_PAYLOAD / "lean/lake-manifest.json"
    packages = json.loads(manifest_path.read_text())["packages"]
    directory = assets_root / "mathlib-packages"
    if {path.name for path in directory.iterdir()} != {item["name"] for item in packages}:
        raise ValueError("The open dependency directories differ from Lean's lock")
    for item in packages:
        config = configparser.ConfigParser()
        with canonical_path(directory / item["name"] / ".git/config").open() as stream:
            config.read_file(stream)
        if (config.defaults() or set(config.sections()) != {"core", 'remote "origin"'}
                or set(config["core"]) - {"bare", "filemode", "logallrefupdates", "repositoryformatversion"}
                or set(config['remote "origin"']) - {"url", "fetch", "partialclonefilter", "promisor"}
                or config['remote "origin"']["url"] != item["url"]):
            raise ValueError("Unrecognized Git configuration for open dependency: " + item["name"])


def public_asset_member(member):
    parts = Path(member.name).parts
    if ".git" in parts:
        relative = parts[parts.index(".git") + 1:]
        if relative and relative[0] in {"logs", "hooks", "FETCH_HEAD"}:
            # These are author-machine tracking data. Lake needs the checkout
            # revision and object database, not local reflogs or hook files.
            return None
    return member


def write_archive(path, release, files, asset_dirs, image):
    partial = path.with_name(path.name + ".partial")
    with partial.open("xb") as stream:
        try:
            with tarfile.open(fileobj=stream, mode="w:gz", compresslevel=1) as archive:
                for name in files:
                    archive.add(canonical_path(release / name), arcname=f"{DIST}/{name}", recursive=False)
                for name in asset_dirs:
                    archive.add(_checked_tree(PREVIOUS / name), arcname=f"{DIST}/{name}",
                                filter=public_asset_member)
                for name, value in archive_metadata(release, asset_dirs, image).items():
                    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
                    info = tarfile.TarInfo(f"{DIST}/{name}")
                    info.mode = 0o644
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            partial.rename(path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise


def assemble_archives(*, release=RELEASE, source_only=False, allow_unvalidated=False):
    """Publish recipes and selected open dependencies without native runner binaries."""
    release = canonical_path(Path(release).absolute())
    image = None if allow_unvalidated else validated_image(release)
    files = [*TEMPLATE_FILES, "BUILD.md", "README.md", "PROTOCOL.md", "SCORING.md", "STATE_USAGE.md",
             "STATE_FORMAT.md", "task_groups.json", "exact_reference_catalogue.json",
             "HARNESS_SOURCES.md", "harness_registry.json", "source-manifest.json",
             "build-zcode-assets.py", "native-package-lock.json", *sealed_files(release)]
    if image is not None:
        files.append("validation-summary.json")
    products = [(f"{DIST}-github-source.tar.gz", ())]
    if not source_only:
        check_public_git_assets(release, PREVIOUS)
        products.append((f"{DIST}-public-build-context.tar.gz", PUBLIC_ASSET_DIRS))
    for name in files:
        path = canonical_path(release / name)
        if not path.is_file():
            raise FileNotFoundError(f"Missing release component: {path}")
    for name, asset_dirs in products:
        if (release.parent / name).exists():
            raise FileExistsError("Refusing to overwrite an existing release archive: " + name)
        for directory in asset_dirs:
            if not (PREVIOUS / directory).is_dir():
                raise FileNotFoundError(f"Missing public build assets: {directory}")
    records = []
    for name, asset_dirs in products:
        path = release.parent / name
        write_archive(path, release, files, asset_dirs, image)
        multipart = split_artifact(path)
        record = {"path": name, "bytes": path.stat().st_size,
                  "sha256": multipart["sha256"] if multipart else sha(path)}
        if multipart is not None:
            record["multipart"] = multipart
        records.append(record)
        print(json.dumps(record), flush=True)
    checksums = []
    for record in records:
        checksums.append(record["sha256"] + "  " + Path(record["path"]).name + "\n")
        if multipart := record.get("multipart"):
            checksums.append(multipart["manifest_sha256"] + "  " + multipart["manifest"] + "\n")
            checksums.extend(part["sha256"] + "  " + part["name"] + "\n" for part in multipart["parts"])
    if any(record.get("multipart") for record in records):
        for suffix in ("py", "ps1"):
            restore_script = release.parent / (f"{DIST}-restore-artifact." + suffix)
            if restore_script.exists():
                raise FileExistsError("A multipart restore script already exists")
            copy_file(release / ("restore-artifact." + suffix), restore_script)
            checksums.append(sha(restore_script) + "  " + restore_script.name + "\n")
    canonical_path(release.parent / f"{DIST}-artifacts.json").write_text(json.dumps(records, indent=2) + "\n")
    canonical_path(release.parent / f"{DIST}-SHA256SUMS").write_text("".join(checksums))
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--without-assets", action="store_true")
    parser.add_argument("--archives", action="store_true")
    parser.add_argument("--source-only", action="store_true", help="With --archives, omit the open-dependency build context")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    release = args.output or RELEASE
    if args.archives:
        if args.without_assets:
            parser.error("--without-assets controls staging. --source-only controls archive assembly.")
        assemble_archives(release=release, source_only=args.source_only)
    else:
        if args.source_only:
            parser.error("--source-only requires --archives.")
        manifest = build(include_assets=not args.without_assets, release=release)
        print(json.dumps({"source_files": len(manifest["files"]), "context": str(release),
                          "manifest_sha256": sha(release / "source-manifest.json")}))


if __name__ == "__main__":
    main()
