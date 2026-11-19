"""Exercise the real Bash launcher with local fake engines and real checksums.

No container engine, model API or global engine store is contacted. The reduced
PATH also exercises the macOS shasum branch without requiring a macOS host.
"""
from __future__ import annotations

import hashlib
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[3]
CHECKOUT = PROJECT.parents[2] if PROJECT.parent.parent.name == "payload" else None
RELEASE = CHECKOUT or PROJECT / "releases/seekgnomes-v1.3.0"
LAUNCHER = RELEASE / "seekgnomes"
EXPECTED = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
FAKE_KEY = "UNIQUE_FAKE_API_KEY_ONLY_FOR_LAUNCHER_TEST"

ENGINE = r'''import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ['MOCK_CALLS']).open('a') as stream:
    stream.write(json.dumps({'engine':Path(sys.argv[0]).name,'args':args})+'\n')
state = Path(os.environ['MOCK_STATE'])
canonical = Path(os.environ['MOCK_CANONICAL_STATE'])
if args[:2] == ['image','inspect']:
    selected = canonical if args[-1].startswith('localhost/') else state
    if not selected.exists():
        raise SystemExit(1)
    print(selected.read_text().strip())
elif args and args[0] == 'load':
    canonical.write_text(os.environ['MOCK_LOAD_ID'])
    if os.environ.get('MOCK_CANONICAL_ONLY') != '1':
        state.write_text(os.environ['MOCK_LOAD_ID'])
elif args and args[0] == 'build':
    state.write_text(os.environ['MOCK_LOAD_ID'])
elif args and args[0] == 'tag':
    if args[1:] != ['localhost/seekgnomes:1.3.0','seekgnomes:1.3.0'] or not canonical.exists():
        raise SystemExit(11)
    state.write_text(canonical.read_text())
elif args and args[0] == 'info':
    print('["name=rootless"]' if os.environ.get('MOCK_ROOTLESS')=='1' else '[]')
elif args and args[0] == 'run':
    if not state.exists() or state.read_text().strip() != os.environ['MOCK_LOAD_ID']:
        raise SystemExit(12)
    print('MOCK_CONTAINER_STARTED')
else:
    raise SystemExit(9)
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".launcher-tests-", dir=RELEASE)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root / "release with spaces"
        self.release.mkdir()
        shutil.copyfile(LAUNCHER, self.release / "seekgnomes")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("bash", "dirname", "cat", "mkdir", "grep"):
            target = shutil.which(name)
            self.assertIsNotNone(target)
            (self.bin / name).symlink_to(Path(target).resolve())
        # Host identity is an input to the launcher. Model it explicitly so
        # these tests also run in CI whose own test process happens to be root.
        self.normal_uid = os.getuid() or 1000
        self.normal_gid = os.getgid() or 1000
        mock_id = self.bin / "id"
        mock_id.write_text('''#!/bin/bash
case "$1" in
  -u) printf '%s\\n' "$MOCK_HOST_UID" ;;
  -g) printf '%s\\n' "$MOCK_HOST_GID" ;;
  *) exit 9 ;;
esac
''')
        mock_id.chmod(0o755)
        for name in ("docker", "podman"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + ENGINE)
            path.chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.state = self.root / "image-state"
        self.canonical = self.root / "canonical-image-state"
        self.marker = self.root / "checksum-tool"
        self.output = self.root / "results with spaces"
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.startswith("SEEKGNOMES_") or key.startswith("MOCK_"):
                self.env.pop(key)
        self.env.update(PATH=str(self.bin), SEEKGNOMES_ENGINE="docker",
                        SEEKGNOMES_API_KEY=FAKE_KEY,
                        SEEKGNOMES_RESULTS_DIR=str(self.output),
                        MOCK_CALLS=str(self.calls), MOCK_STATE=str(self.state),
                        MOCK_CANONICAL_STATE=str(self.canonical),
                        MOCK_LOAD_ID=EXPECTED, MOCK_ROOTLESS="0",
                        MOCK_HOST_UID=str(self.normal_uid), MOCK_HOST_GID=str(self.normal_gid),
                        MOCK_CHECKSUM_MARKER=str(self.marker),
                        PYTHONDONTWRITEBYTECODE="1")
        (self.release / "image-id.txt").write_text(EXPECTED + "\n")
        self.state.write_text(EXPECTED)
        self.checksum_tool("sha256sum")

    def checksum_tool(self, name):
        actual = shutil.which(name)
        if actual is None:
            self.skipTest("Checksum utility not installed: " + name)
        wrapper = self.bin / name
        wrapper.write_text("#!/bin/bash\nprintf '%s\\n' " + name
                           + " >> \"$MOCK_CHECKSUM_MARKER\"\nexec "
                           + str(Path(actual).resolve()) + ' "$@"\n')
        wrapper.chmod(0o755)

    def archive(self, *, corrupt=False):
        blob = b"A local mock image archive; no engine reads its contents.\n"
        (self.release / "seekgnomes-image.tar.gz").write_bytes(blob)
        digest = "0" * 64 if corrupt else hashlib.sha256(blob).hexdigest()
        (self.release / "image.sha256").write_text(digest + "  seekgnomes-image.tar.gz\n")

    def run_launcher(self, *args):
        result = subprocess.run(["/bin/bash", str(self.release / "seekgnomes"), *args],
                                env=self.env, capture_output=True, text=True, timeout=20)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, calls

    def run_args(self, calls):
        records = [record["args"] for record in calls if record["args"][:1] == ["run"]]
        self.assertEqual(len(records), 1)
        return records[0]

    def test_existing_image_forwards_arguments_and_space_paths_without_key_argv(self):
        result, calls = self.run_launcher("run", "--model", "model with spaces", "--tasks", "1,7")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.run_args(calls)
        self.assertEqual(args[-6:], ["seekgnomes:1.3.0", "run", "--model", "model with spaces", "--tasks", "1,7"])
        mount = args[args.index("-v") + 1]
        self.assertEqual(mount, str(self.output) + ":/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs:rw")
        self.assertEqual(args[args.index("-e") + 1], "SEEKGNOMES_API_KEY")
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertNotIn("--cap-add", args)
        self.assertNotIn(FAKE_KEY, json.dumps(calls))
        self.assertFalse(any(record["args"][0] in ("load", "build") for record in calls))

    def test_first_load_verifies_real_checksum_then_starts(self):
        self.state.unlink()
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text().strip(), "sha256sum")
        self.assertEqual(sum(record["args"][0] == "load" for record in calls), 1)
        self.assertEqual(self.run_args(calls)[-1], "doctor")

    def test_cli_cpu_allocation_matches_engine_even_with_different_env_default(self):
        self.env["SEEKGNOMES_CPUS"] = "2"
        result, calls = self.run_launcher("run", "--cpus", "4", "--model", "gpt-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.run_args(calls)
        self.assertEqual(args[args.index("--cpus") + 1], "4")
        self.assertEqual(args[-5:], ["run", "--cpus", "4", "--model", "gpt-fixture"])

    def test_cpu_equals_syntax_and_invalid_allocation(self):
        result, calls = self.run_launcher("doctor", "--cpus=3")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.run_args(calls)
        self.assertEqual(args[args.index("--cpus") + 1], "3")
        self.calls.unlink()
        result, calls = self.run_launcher("run", "--cpus", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_wrong_cached_image_is_reloaded_to_release_id(self):
        self.state.write_text(OTHER)
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), EXPECTED)
        self.assertEqual(sum(record["args"][0] == "load" for record in calls), 1)
        self.run_args(calls)

    def test_canonical_only_archive_is_retagged_before_running(self):
        self.state.unlink()
        self.env["MOCK_CANONICAL_ONLY"] = "1"
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.canonical.read_text(), EXPECTED)
        self.assertEqual(self.state.read_text(), EXPECTED)
        tags = [record["args"] for record in calls if record["args"][0] == "tag"]
        self.assertEqual(tags, [["tag", "localhost/seekgnomes:1.3.0", "seekgnomes:1.3.0"]])
        operations = [record["args"][0] for record in calls]
        self.assertLess(operations.index("load"), operations.index("tag"))
        self.assertLess(operations.index("tag"), operations.index("run"))
        self.run_args(calls)

    def test_canonical_archive_overwrites_wrong_cached_short_tag(self):
        self.state.write_text(OTHER)
        self.env["MOCK_CANONICAL_ONLY"] = "1"
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), EXPECTED)
        self.assertEqual(sum(record["args"][0] == "tag" for record in calls), 1)
        self.run_args(calls)

    def test_still_wrong_id_after_loading_never_runs(self):
        self.state.unlink()
        self.env["MOCK_LOAD_ID"] = OTHER
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match this release", result.stderr)
        self.assertFalse(any(record["args"][0] == "run" for record in calls))

    def test_corrupt_archive_never_loads_or_runs(self):
        self.state.unlink()
        self.archive(corrupt=True)
        result, calls = self.run_launcher("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(record["args"][0] in ("run", "load") for record in calls))

    def test_mac_style_path_uses_real_shasum_fallback(self):
        (self.bin / "sha256sum").unlink()
        self.checksum_tool("shasum")
        self.state.unlink()
        self.archive()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text().strip(), "shasum")
        self.run_args(calls)

    def test_explicit_rootless_docker_is_rejected_without_running(self):
        self.env["MOCK_ROOTLESS"] = "1"
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Rootless Docker", result.stderr)
        self.assertFalse(any(record["args"][0] in ("run", "load", "build") for record in calls))

    def test_host_root_is_rejected_before_image_or_container_actions(self):
        self.env.update(MOCK_HOST_UID="0", MOCK_HOST_GID="0")
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-root host user", result.stderr)
        self.assertEqual(calls, [])

    def test_auto_engine_switches_from_rootless_docker_to_podman(self):
        self.env.pop("SEEKGNOMES_ENGINE")
        self.env["MOCK_ROOTLESS"] = "1"
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [record for record in calls if record["args"][:1] == ["run"]]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["engine"], "podman")
        self.assertIn("--userns=keep-id", runs[0]["args"])
        self.assertIn("unmask=ALL", runs[0]["args"])

    def test_auto_rootless_docker_without_podman_stops_clearly(self):
        self.env.pop("SEEKGNOMES_ENGINE")
        self.env["MOCK_ROOTLESS"] = "1"
        (self.bin / "podman").unlink()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Rootless Docker", result.stderr)
        self.assertFalse(any(record["args"][0] in ("run", "load", "build") for record in calls))

    def test_rootful_docker_preserves_host_uid_and_systempaths(self):
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.run_args(calls)
        users = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--user"]
        self.assertEqual(users, [f"{self.normal_uid}:{self.normal_gid}"])
        self.assertNotIn("--userns=keep-id", args)
        self.assertIn("systempaths=unconfined", args)

    def test_podman_preserves_uid_and_unmasks_nested_proc(self):
        self.env["SEEKGNOMES_ENGINE"] = "podman"
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.run_args(calls)
        self.assertIn("--userns=keep-id", args)
        self.assertIn("unmask=ALL", args)
        self.assertNotIn("systempaths=unconfined", args)
        self.assertTrue(all(record["engine"] == "podman" for record in calls))

    def test_source_distribution_build_branch_uses_release_directory(self):
        self.state.unlink()
        (self.release / "image-id.txt").unlink()
        result, calls = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [record["args"] for record in calls if record["args"][0] == "build"]
        self.assertEqual(builds, [["build", "-t", "seekgnomes:1.3.0", str(self.release)]])
        self.run_args(calls)


class RuntimeAssetTests(unittest.TestCase):
    def test_runtime_role_prompts_are_in_the_release_source_closure(self):
        sys.path.insert(0, str(PROJECT))
        from revisions.seekgnomes_v1_2.build_bundle import source_closure
        names = set(source_closure())
        for role in ('constructor', 'formalist'):
            relative = 'revisions/seekgnomes_v1_1_1/prompts/' + role + '.md'
            self.assertIn(relative, names)
            self.assertGreater((PROJECT / relative).stat().st_size, 0)

    def test_multipart_reconstruction_verifies_parts_and_rejects_corruption(self):
        sys.path.insert(0, str(PROJECT))
        from revisions.seekgnomes_v1_2.build_bundle import split_artifact
        spec = importlib.util.spec_from_file_location("seekgnomes_restore_test", RELEASE / "restore-artifact.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix=".multipart-test-", dir=RELEASE) as root:
            archive = Path(root) / "release.tar"
            payload = bytes(range(128))
            archive.write_bytes(payload)
            record = split_artifact(archive, part_bytes=31)
            self.assertEqual(len(record["parts"]), 5)
            archive.unlink()
            manifest = Path(root) / record["manifest"]
            with contextlib.redirect_stdout(io.StringIO()):
                restored = module.restore(manifest)
            self.assertEqual(restored.read_bytes(), payload)
            restored.unlink()
            part = Path(root) / record["parts"][0]["name"]
            part.write_bytes(part.read_bytes() + b"corruption")
            with self.assertRaises(ValueError):
                module.restore(manifest)
            self.assertFalse(archive.exists())
            self.assertFalse(archive.with_name(archive.name + ".restore-partial").exists())

    def test_preparing_context_keeps_frozen_source_single_link(self):
        sys.path.insert(0, str(PROJECT))
        from revisions.seekgnomes_v1_2.build_bundle import copytree_exact
        with tempfile.TemporaryDirectory(prefix=".asset-copy-test-", dir=RELEASE) as root:
            source, destination = Path(root) / "source", Path(root) / "context"
            source.mkdir()
            original = source / "verifier"
            original.write_bytes(b"a frozen verifier fixture")
            original.chmod(0o755)
            copytree_exact(source, destination)
            copied = destination / "verifier"
            self.assertEqual(original.stat().st_nlink, 1)
            self.assertEqual(copied.stat().st_nlink, 1)
            self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
            self.assertEqual(original.read_bytes(), copied.read_bytes())
            copied.write_bytes(b"a replaced context fixture")
            self.assertEqual(original.read_bytes(), b"a frozen verifier fixture")

    def test_runtime_asset_cannot_follow_links_outside_its_tree(self):
        sys.path.insert(0, str(PROJECT))
        from revisions.seekgnomes_v1_2.build_bundle import copytree_exact
        with tempfile.TemporaryDirectory(prefix=".asset-link-test-", dir=RELEASE) as root:
            source = Path(root) / "source"
            source.mkdir()
            other = Path(root) / "outside"
            other.write_text("not a runtime asset")
            (source / "escape").symlink_to(other)
            with self.assertRaises(ValueError):
                copytree_exact(source, Path(root) / "context")


if __name__ == "__main__":
    unittest.main(verbosity=2)
