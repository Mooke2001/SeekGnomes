import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from revisions.seekgnomes_v1_2.task_export import LEGACY_PROTOCOL, PROJECT, STATE_PROTOCOL, export_tasks


class ExportTests(unittest.TestCase):
    def test_fifty_fixed_tasks_and_no_outer_formalist_timeout(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "tmp") as directory:
            result = export_tasks(Path(directory) / "tasks")
            self.assertEqual(len(result["tasks"]), 50)
            self.assertFalse(result["contains_credentials"])
            self.assertEqual(result["protocol"], STATE_PROTOCOL)
            for row in result["tasks"]:
                root = Path(row["path"])
                cfg = tomllib.loads((root / "task.toml").read_text())
                self.assertNotIn("timeout_sec", cfg["agent"])
                task = json.loads((root / "task.json").read_text())
                self.assertEqual(task["canonical_sha256"], row["task_sha256"])
                self.assertEqual(cfg["metadata"]["task_sha256"], task["canonical_sha256"])
                self.assertEqual(cfg["metadata"]["candidate_format"], "state")
                self.assertEqual(cfg["metadata"]["protocol"], STATE_PROTOCOL)
                statement = (root / "instruction.md").read_text()
                self.assertIn("COMPLETE f.state", statement)
                self.assertNotIn("f.py", statement)
                self.assertIn("SEEKGNOMES_RECEIPT_VERIFY_KEY", (root / "tests/test.sh").read_text())

    def test_no_alias_or_duplicate_exports(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "tmp") as directory:
            root = Path(directory)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                export_tasks(root / "alias/tasks", [1])
            with self.assertRaises(ValueError):
                export_tasks(root / "duplicate", [1, 1])

    def test_cpu_configuration_is_recorded_in_exported_task_resources(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "tmp") as directory:
            result = export_tasks(Path(directory) / "tasks", [1], cpus=3)
            self.assertEqual(result["cpus"], 3)
            config = tomllib.loads((Path(result["tasks"][0]["path"]) / "task.toml").read_text())
            self.assertEqual(config["environment"]["cpus"], 3)

    def test_legacy_format_is_explicit(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "tmp") as directory:
            result = export_tasks(Path(directory) / "tasks", [1], candidate_format="legacy")
            self.assertEqual(result["protocol"], LEGACY_PROTOCOL)
            root = Path(result["tasks"][0]["path"])
            config = tomllib.loads((root / "task.toml").read_text())
            self.assertEqual(config["metadata"]["candidate_format"], "legacy")
            self.assertIn("definition f.md", (root / "instruction.md").read_text())


if __name__ == "__main__":
    unittest.main()
