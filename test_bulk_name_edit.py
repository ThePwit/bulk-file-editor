import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("bulk_name_edit.py")
SPEC = importlib.util.spec_from_file_location("bulk_name_edit", MODULE_PATH)
bulk_name_edit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bulk_name_edit
SPEC.loader.exec_module(bulk_name_edit)


class BulkNameEditTests(unittest.TestCase):
    def test_parse_size_accepts_spacing_and_large_units(self):
        self.assertEqual(bulk_name_edit.parse_size_str("1 KB"), 1024)
        self.assertEqual(bulk_name_edit.parse_size_str("2tb"), 2 * 1024**4)

    def test_date_only_until_covers_the_whole_day(self):
        start = bulk_name_edit.parse_time_str("2026-05-22")
        end = bulk_name_edit.parse_time_str("2026-05-22", end_of_day=True)

        self.assertGreater(end, start)
        self.assertLess(end - start, 86400)

    def test_validate_new_filename_rejects_unsafe_names(self):
        self.assertIsNone(bulk_name_edit.validate_new_filename("Episode 01.mkv"))
        self.assertIn("folders", bulk_name_edit.validate_new_filename("Season 01/Episode 01.mkv"))
        self.assertIn("reserved", bulk_name_edit.validate_new_filename("CON.txt"))
        self.assertIn("space or dot", bulk_name_edit.validate_new_filename("Episode 01."))

    def test_collect_files_deduplicates_globs_and_normalizes_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wanted = base / "A.TXT"
            wanted.write_text("x", encoding="utf-8")
            (base / "notes.md").write_text("x", encoding="utf-8")

            files = list(bulk_name_edit.collect_files(
                base=base,
                recursive=False,
                globs=["*.*", "*.TXT"],
                exclude_globs=[],
                exts=["TXT"],
                name_contains=None,
                min_size=None,
                max_size=None,
                since_ts=None,
                until_ts=None,
            ))

            self.assertEqual(files, [wanted])

    def test_make_backup_uses_unique_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "note.txt"
            source.write_text("new", encoding="utf-8")
            (base / "note.txt.bak").write_text("old", encoding="utf-8")

            backup = bulk_name_edit.make_backup(source, ".bak")

            self.assertEqual(backup.name, "note.txt.bak.1")
            self.assertEqual(backup.read_text(encoding="utf-8"), "new")

    def test_duplicate_rename_targets_are_marked_as_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            one = base / "one.txt"
            two = base / "two.txt"
            one.write_text("1", encoding="utf-8")
            two.write_text("2", encoding="utf-8")
            plans = [
                bulk_name_edit.FilePlan(one, rename=bulk_name_edit.RenamePlan("same.txt")),
                bulk_name_edit.FilePlan(two, rename=bulk_name_edit.RenamePlan("same.txt")),
            ]

            bulk_name_edit.BulkEditApp._annotate_rename_conflicts(None, plans)

            self.assertIn("Duplicate target", plans[0].skipped_reason)
            self.assertIn("Duplicate target", plans[1].skipped_reason)

    def test_two_phase_rename_handles_swaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            one = base / "one.txt"
            two = base / "two.txt"
            one.write_text("one", encoding="utf-8")
            two.write_text("two", encoding="utf-8")
            app = bulk_name_edit.BulkEditApp.__new__(bulk_name_edit.BulkEditApp)
            app.msg_q = bulk_name_edit.queue.Queue()
            app.last_renames = []

            completed = bulk_name_edit.BulkEditApp._perform_rename_batch(
                app,
                [(one, two), (two, one)],
                base,
                record_undo=True,
                action="RENAME",
            )

            self.assertEqual(len(completed), 2)
            self.assertEqual(one.read_text(encoding="utf-8"), "two")
            self.assertEqual(two.read_text(encoding="utf-8"), "one")
            self.assertEqual(len(app.last_renames), 2)


if __name__ == "__main__":
    unittest.main()
