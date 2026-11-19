"""Build-time validation of native CLI assets before executing their installer."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
if root.resolve(strict=True) != root:
    raise ValueError("Aliased harness asset root")
record = json.loads((root / "manifest.json").read_text())
files = record.get("files")
if not isinstance(files, dict) or not files or "install.sh" not in files:
    raise ValueError("Missing hashed harness installer")
for name, expected in files.items():
    relative = Path(name)
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or path.resolve(strict=True) != path:
        raise ValueError("Noncanonical harness asset")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("Harness asset SHA256 mismatch: " + name)
