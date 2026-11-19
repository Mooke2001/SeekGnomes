"""Assemble the local console around the verified, prebuilt Harbor image."""
import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
VERSION = "1.3.0"
BASE = PROJECT / f"releases/seekgnomes-v{VERSION}"
if HERE.name == "console-src":
    BASE = HERE.parent
ARCHIVE_ROOT = f"seekgnomes-console-v{VERSION}"
IMAGE_ARCHIVE = "seekgnomes-image.tar.gz"
COMPANY_LABELS = {
    "openai": "OpenAI", "anthropic": "Anthropic", "moonshot": "Moonshot / Kimi",
    "google": "Google", "xai": "xAI", "alibaba": "Alibaba / Qwen",
    "deepseek": "DeepSeek", "xiaomi": "Xiaomi / MiMo", "zai": "Z.ai / GLM",
    "other": "Other",
}


def sha(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def package_data(base, endpoint):
    if endpoint:
        value = urlsplit(endpoint)
        if (value.scheme != "https" or not value.hostname or value.username is not None
                or value.password is not None or value.query or value.fragment or value.path != "/v1/submissions"):
            raise ValueError("Use the publisher-owned HTTPS /v1/submissions URL")
    groups = json.loads((base / "task_groups.json").read_text())["tasks"]
    task_file = base / "payload/Projects/QuantumManyBodyVariational/config/benchmark50_tasks.json"
    rows = json.loads(task_file.read_text())["tasks"]
    tasks = [{"id": row["id"], "name": row["name"], "dimension": row["dimension"],
              "group": groups[row["id"]]["group"]} for row in rows]
    if [row["id"] for row in tasks] != [f"{number:02d}" for number in range(1, 51)]:
        raise ValueError("The package must contain the ordered 50-task catalogue")
    registry = json.loads((base / "harness_registry.json").read_text())
    companies = []
    for company, label in COMPANY_LABELS.items():
        driver = registry["companies"].get(company, registry["fallback"])
        driver_info = registry["harnesses"][driver]
        if driver_info["supported"]:
            companies.append({"id": company, "label": label, "harness": driver,
                              "protocols": driver_info["allowed_api_protocols"],
                              "default_protocol": driver_info["api_protocol"]})
    return {
        "console/catalogue.json": json_bytes(tasks),
        "console/companies.json": json_bytes(companies),
        "console/console-config.json": json_bytes({"submission_endpoint": endpoint or None}),
    }


def build(destination, endpoint, *, base=BASE):
    destination = destination.absolute()
    if destination.resolve() != destination:
        raise ValueError("Write the archive to an unaliased path")
    if destination.exists():
        raise FileExistsError(destination)
    image = base / IMAGE_ARCHIVE
    image_id = (base / "image-id.txt").read_text().strip().removeprefix("sha256:")
    image_check = (base / "image.sha256").read_text().split()
    if (not re.fullmatch(r"[0-9a-f]{64}", image_id) or len(image_check) != 2
            or image_check[1] != IMAGE_ARCHIVE or sha(image) != image_check[0]):
        raise ValueError("The Harbor image did not match its sealed image digest")
    manifest = base / "source-manifest.json"
    record = json.loads((base / "validation-summary.json").read_text())
    if (json.loads(manifest.read_text()).get("version") != VERSION or record.get("ok") is not True
            or str(record.get("image_id", "")).removeprefix("sha256:") != image_id
            or record.get("source_manifest_sha256") != sha(manifest)):
        raise ValueError("The image and source must be bound to a successful release validation")
    generated = package_data(base, endpoint)
    sources = {
        "Start.command": HERE / "Start.command",
        "Start WSL.cmd": HERE / "Start WSL.cmd",
        "README.md": HERE / "README.md",
        "SHARING.md": HERE / "SHARING.md",
        "BASE-RUNTIME.md": base / "README.md",
    }
    for name in ("server.py", "runner.py", "sharing.py", "receiver.py"):
        sources["console/" + name] = HERE / name
    for name in ("index.html", "style.css", "app.js"):
        sources["console/ui/" + name] = HERE / "ui" / name
    for name in ("runtime_entry.py", "parallel_campaign.py"):
        sources["console/runtime/" + name] = HERE / "runtime" / name
    for name in ("seekgnomes", "seekgnomes.ps1", "image-id.txt", "image.sha256", "source-manifest.json",
                 "validation-summary.json", "THIRD_PARTY_NOTICES.md", "PROTOCOL.md", "SCORING.md",
                 "seekgnomes-image.tar.gz"):
        sources[name] = base / name
    if any(not file.is_file() or file.resolve() != file for file in sources.values()):
        raise ValueError("A required package file is missing or aliased")
    provenance = {name: image_check[0] if file == image else sha(file) for name, file in sources.items()}
    provenance.update({name: sha256(body).hexdigest()
                       for name, body in generated.items()})
    generated["console-manifest.json"] = json_bytes({"schema": "seekgnomes-console-package/v1",
                                                    "base_image_id": "sha256:" + image_id, "sha256": provenance})
    temporary = destination.with_suffix(destination.suffix + ".partial")
    with temporary.open("xb") as stream:
        try:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name, path in sources.items():
                    archive.add(path, arcname=f"{ARCHIVE_ROOT}/{name}", recursive=False)
                for name, data in generated.items():
                    meta = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{name}")
                    meta.size = len(data)
                    archive.addfile(meta, io.BytesIO(data))
            temporary.rename(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    print(f"{sha(destination)}  {destination.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=BASE, help="Validated release staging directory")
    parser.add_argument("--submission-endpoint")
    args = parser.parse_args()
    build(args.out, args.submission_endpoint, base=args.base.absolute())


if __name__ == "__main__":
    main()
