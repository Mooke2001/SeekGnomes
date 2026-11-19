"""Restore a multipart release, checking every part and the complete SHA-256.

Usage: python3 seekgnomes-v1.2.0-restore-artifact.py FILE.parts.json
Requires only Python's standard library; never accesses a network or API.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def restore(manifest_path):
    manifest_path = Path(manifest_path).resolve(strict=True)
    record = json.loads(manifest_path.read_text())
    if record.get("schema") != "seekgnomes-multipart/v1":
        raise ValueError("Unknown multipart release format")
    name = record["name"]
    if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("Unsafe restored archive name")
    destination = manifest_path.parent / name
    temporary = destination.with_name(destination.name + ".restore-partial")
    if destination.exists() or destination.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise FileExistsError("Restore destination already exists: " + str(destination))
    whole, total, seen = hashlib.sha256(), 0, set()
    try:
        with temporary.open("xb") as output:
            for index, part in enumerate(record["parts"], 1):
                part_name = part["name"]
                if part_name != name + f".part{index:03d}" or part_name in seen:
                    raise ValueError("Unexpected or duplicate multipart filename")
                seen.add(part_name)
                path = manifest_path.parent / part_name
                if path.resolve(strict=True) != path or not path.is_file():
                    raise ValueError("Missing or aliased multipart file: " + part_name)
                part_digest, part_size = hashlib.sha256(), 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        output.write(block)
                        part_digest.update(block)
                        whole.update(block)
                        part_size += len(block)
                if part_size != part["bytes"] or part_digest.hexdigest() != part["sha256"]:
                    raise ValueError("Multipart SHA-256/size mismatch: " + part_name)
                total += part_size
        if not seen or total != record["bytes"] or whole.hexdigest() != record["sha256"]:
            raise ValueError("Complete restored archive SHA-256/size mismatch")
        temporary.rename(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(json.dumps({"path": str(destination), "bytes": total, "sha256": whole.hexdigest()}))
    return destination


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 seekgnomes-v1.2.0-restore-artifact.py FILE.parts.json")
    restore(sys.argv[1])
