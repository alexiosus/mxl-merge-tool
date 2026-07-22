from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mxl_tool import (
    MAGIC,
    MXL_ATTRIBUTES_LINE,
    TEXTCONV_FORMAT_VERSION,
    UTF8_BOM,
    _ensure_attributes_file,
    atomic_write_bytes,
    driver_report_path,
    install_git_config,
    merge_documents,
    parse_document,
    resolve_documents,
    semantic_coordinates,
    semantic_entries,
    semantic_values,
    textconv,
)


PREFIX = MAGIC + b"\x00\x08\x00\x01\x00\x0c\x00" + UTF8_BOM
REF_UUID = "bd33005056b8451711f0059f5d582b65"


def make_mxl(values: list[str], type_index: int = 53) -> bytes:
    serialized_values = ",\n".join(
        f'{{"#","{value.replace(chr(34), chr(34) * 2)}"}}' for value in values
    )
    body = (
        "{8,1,12,\n"
        f'{{"#",a1af1af2-f26f-40c9-a516-a66ff64531ed,{type_index}:{REF_UUID}}},\n'
        f"{serialized_values}\n"
        "}"
    )
    return PREFIX + body.encode("utf-8")


def make_row_record_mxl(rows: list[tuple[int, str]]) -> bytes:
    records = []
    for coordinate, value in rows:
        escaped = value.replace(chr(34), chr(34) * 2)
        records.extend((str(coordinate), "0", "1", "0", f'{{"#","{escaped}"}}'))
    return PREFIX + ("{" + ",".join((*records, "99", "0")) + "}").encode("utf-8")


class MxlToolTests(unittest.TestCase):
    def document(self, values: list[str], type_index: int = 53, name: str = "test.mxl"):
        return parse_document(make_mxl(values, type_index), name)

    def test_extracts_semantic_values(self):
        document = self.document(["Alpha", "A \"quoted\" value"])

        self.assertEqual(["Alpha", "A \"quoted\" value"], semantic_values(document))
        self.assertEqual(
            ["Alpha", "A \"quoted\" value"],
            [value for _, value in semantic_entries(document)],
        )
        self.assertTrue(all(index > 0 for index, _ in semantic_entries(document)))
        self.assertEqual(["R1C1", "R2C1"], semantic_coordinates(document))
        self.assertIn("Alpha", textconv(document))

    def test_extracts_real_row_and_column_coordinates(self):
        def cell(value: str) -> str:
            return f'{{"#","{value}"}}'

        data = PREFIX + (
            "{"
            + ",".join(
                (
                    "0", "0", "2", "0", cell("Alpha"), "2", cell("Gamma"),
                    "3", "0", "1", "0", cell("Delta"),
                    "99", "0",
                )
            )
            + "}"
        ).encode("utf-8")
        document = parse_document(data)

        self.assertEqual(["Alpha", "Gamma", "Delta"], semantic_values(document))
        self.assertEqual(
            ["R1C1", "R1C3", "R4C1"],
            semantic_coordinates(document),
        )

    def test_rejects_multiple_roots_and_missing_values(self):
        for payload in ("{}{}", "{,,}", "{value,}"):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "token|value"):
                    parse_document(PREFIX + payload.encode("utf-8"))

    def test_textconv_includes_hidden_atoms(self):
        first = parse_document(make_mxl(["Alpha"]).replace(b"{8,1,12,", b"{8,1,99,"))
        second = parse_document(make_mxl(["Alpha"]))

        self.assertNotEqual(textconv(first), textconv(second))
        self.assertIn("A 99", textconv(first))

    def test_global_attributes_are_added_once(self):
        with tempfile.TemporaryDirectory() as directory:
            attributes = Path(directory) / "git" / "attributes"
            attributes.parent.mkdir(parents=True)
            attributes.write_text("*.txt text\n", encoding="utf-8")

            _ensure_attributes_file(attributes)
            _ensure_attributes_file(attributes)

            lines = attributes.read_text(encoding="utf-8").splitlines()

        self.assertEqual(1, lines.count(MXL_ATTRIBUTES_LINE))
        self.assertIn("*.txt text", lines)

    def test_installer_removes_stale_batch_command_and_keeps_reports_in_git_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            previous = Path.cwd()
            os.chdir(root)
            try:
                install_git_config(
                    onec_client="1cv8c.exe",
                    onec_epf="custom.epf",
                    onec_batch_capable=True,
                )
                self.assertTrue(
                    subprocess.run(
                        ["git", "config", "--get", "mxl.previewBatchCommand"],
                        check=False,
                        capture_output=True,
                    ).stdout
                )
                textconv_command = subprocess.check_output(
                    ["git", "config", "--get", "diff.mxl.textconv"], text=True
                )
                self.assertIn(
                    f"--format-version {TEXTCONV_FORMAT_VERSION}", textconv_command
                )

                install_git_config(
                    onec_client="1cv8c.exe",
                    onec_epf="legacy.epf",
                    onec_batch_capable=False,
                )
                configured = subprocess.run(
                    ["git", "config", "--get", "mxl.previewBatchCommand"],
                    check=False,
                    capture_output=True,
                )
                report = driver_report_path("folder/sample.mxl")
            finally:
                os.chdir(previous)

        self.assertNotEqual(0, configured.returncode)
        self.assertIn(".git", report.parts)
        self.assertNotIn("folder", report.parts)

    def test_end_to_end_git_merge_keeps_conflict_report_out_of_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            document = root / "folder with spaces" / "sample file.mxl"
            document.parent.mkdir(parents=True)
            document.write_bytes(make_mxl(["Base"]))
            previous = Path.cwd()
            os.chdir(root)
            try:
                install_git_config()
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-qm", "base"], check=True)
                subprocess.run(["git", "switch", "-qc", "local"], check=True)
                document.write_bytes(make_mxl(["Local"]))
                subprocess.run(["git", "commit", "-qam", "local"], check=True)
                subprocess.run(["git", "switch", "-qc", "remote", "HEAD~1"], check=True)
                document.write_bytes(make_mxl(["Remote"]))
                subprocess.run(["git", "commit", "-qam", "remote"], check=True)
                subprocess.run(["git", "switch", "-q", "local"], check=True)
                merged = subprocess.run(
                    ["git", "merge", "remote"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                os.chdir(previous)

            reports = list((root / ".git" / "mxl-merge" / "reports").glob("*.json"))
            worktree_reports = list(root.rglob("*.merge-conflict.json"))

        self.assertNotEqual(0, merged.returncode)
        self.assertEqual(1, len(reports))
        self.assertEqual([], worktree_reports)

    def test_merges_non_overlapping_value_changes(self):
        base = self.document(["Alpha", "Beta"], name="base")
        local = self.document(["Alpha local", "Beta"], name="local")
        remote = self.document(["Alpha", "Beta remote"], name="remote")

        result = merge_documents(base, local, remote)

        self.assertTrue(result.success)
        assert result.data is not None
        merged = parse_document(result.data)
        self.assertEqual(["Alpha local", "Beta remote"], semantic_values(merged))

    def test_explicit_choice_overrides_automatic_value_merge(self):
        base = self.document(["Automatic", "Conflict"])
        local = self.document(["Local automatic", "Local conflict"])
        remote = self.document(["Automatic", "Remote conflict"])
        automatic_key = str(semantic_entries(base)[0][0])
        conflict = merge_documents(base, local, remote).conflicts[0]

        result = resolve_documents(
            base,
            local,
            remote,
            {
                automatic_key: {"choice": "base"},
                str(conflict["token_index"]): {"choice": "remote"},
            },
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertEqual(
            ["Automatic", "Remote conflict"],
            semantic_values(parse_document(result.data)),
        )

    def test_explicit_choice_overrides_fully_automatic_merge(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha local"])
        remote = self.document(["Alpha"])
        key = str(semantic_entries(base)[0][0])

        result = resolve_documents(
            base, local, remote, {key: {"choice": "base"}}
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertEqual(["Alpha"], semantic_values(parse_document(result.data)))

    def test_whole_document_choice_returns_exact_selected_input(self):
        base = self.document(["Base", "Shared"])
        local = self.document(["Local", "Shared"])
        remote = self.document(["Remote", "Remote only"])

        for choice, expected in (
            ("base", base.data),
            ("local", local.data),
            ("remote", remote.data),
        ):
            result = resolve_documents(
                base,
                local,
                remote,
                {"document": {"choice": choice}},
            )

            self.assertTrue(result.success)
            self.assertEqual(expected, result.data)

    def test_reports_conflicting_value_change(self):
        base = self.document(["Alpha"])
        local = self.document(["Local"])
        remote = self.document(["Remote"])

        result = merge_documents(base, local, remote)

        self.assertFalse(result.success)
        self.assertEqual("Alpha", result.conflicts[0]["base"])
        self.assertEqual("Local", result.conflicts[0]["local"])
        self.assertEqual("Remote", result.conflicts[0]["remote"])

    def test_resolves_value_conflict_with_each_side_or_manual_value(self):
        base = self.document(["Alpha"])
        local = self.document(["Local"])
        remote = self.document(["Remote"])
        conflict = merge_documents(base, local, remote).conflicts[0]
        key = str(conflict["token_index"])

        expected_values = {
            "base": "Alpha",
            "local": "Local",
            "remote": "Remote",
            "manual": 'Manual "merged" value',
        }
        for choice, expected in expected_values.items():
            resolution = {"choice": choice}
            if choice == "manual":
                resolution["value"] = expected
            result = resolve_documents(base, local, remote, {key: resolution})

            self.assertTrue(result.success)
            assert result.data is not None
            self.assertEqual([expected], semantic_values(parse_document(result.data)))

    def test_requires_every_value_conflict_to_be_resolved(self):
        base = self.document(["Alpha", "Beta"])
        local = self.document(["Local Alpha", "Local Beta"])
        remote = self.document(["Remote Alpha", "Remote Beta"])
        conflicts = merge_documents(base, local, remote).conflicts
        first_key = str(conflicts[0]["token_index"])

        result = resolve_documents(
            base, local, remote, {first_key: {"choice": "local"}}
        )

        self.assertFalse(result.success)
        self.assertIn("1 MXL conflict", result.reason)

    def test_resolves_row_operations_by_selecting_remote_structure(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha", "Local row"])
        remote = self.document(["Remote row", "Alpha"])

        conflicts = merge_documents(base, local, remote).conflicts
        resolutions = {
            str(conflict["key"]): {"choice": "remote"}
            for conflict in conflicts
            if conflict["kind"] == "row"
        }

        result = resolve_documents(base, local, remote, resolutions)

        self.assertTrue(result.success)
        self.assertEqual(remote.data, result.data)

    def test_row_addition_can_be_kept_or_omitted_while_merging_field_changes(self):
        base = self.document(["Alpha", "Beta"])
        local = self.document(["Alpha", "Inserted", "Beta"])
        remote = self.document(["Alpha", "Beta remote"])
        conflict = merge_documents(base, local, remote).conflicts[0]

        kept = resolve_documents(
            base, local, remote, {str(conflict["key"]): {"choice": "local"}}
        )
        omitted = resolve_documents(
            base, local, remote, {str(conflict["key"]): {"choice": "base"}}
        )

        self.assertTrue(kept.success)
        self.assertTrue(omitted.success)
        assert kept.data is not None and omitted.data is not None
        self.assertEqual(
            ["Alpha", "Inserted", "Beta remote"],
            semantic_values(parse_document(kept.data)),
        )
        self.assertEqual(
            ["Alpha", "Beta remote"], semantic_values(parse_document(omitted.data))
        )

    def test_row_deletion_can_be_accepted_or_rejected(self):
        base = self.document(["Alpha", "Beta"])
        local = self.document(["Beta"])
        remote = self.document(["Alpha remote", "Beta"])
        conflict = merge_documents(base, local, remote).conflicts[0]

        deleted = resolve_documents(
            base, local, remote, {str(conflict["key"]): {"choice": "local"}}
        )
        kept = resolve_documents(
            base, local, remote, {str(conflict["key"]): {"choice": "remote"}}
        )

        assert deleted.data is not None and kept.data is not None
        self.assertEqual(["Beta"], semantic_values(parse_document(deleted.data)))
        self.assertEqual(
            ["Alpha remote", "Beta"], semantic_values(parse_document(kept.data))
        )

    def test_merges_values_with_real_flat_row_record_layout(self):
        base = parse_document(
            make_row_record_mxl([(0, "Alpha"), (1, "Beta"), (2, "Gamma")]),
            "base",
        )
        local = parse_document(
            make_row_record_mxl([(0, "Alpha"), (2, "Gamma")]), "local"
        )
        remote = parse_document(
            make_row_record_mxl(
                [(0, "Alpha remote"), (1, "Beta"), (2, "Gamma")]
            ),
            "remote",
        )
        conflict = merge_documents(base, local, remote).conflicts[0]

        result = resolve_documents(
            base, local, remote, {str(conflict["key"]): {"choice": "local"}}
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertEqual(
            ["Alpha remote", "Gamma"], semantic_values(parse_document(result.data))
        )

    def test_incompatible_mixed_row_layout_is_rejected(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha", "Local row"])
        remote = self.document(["Remote row", "Alpha"])
        conflicts = merge_documents(base, local, remote).conflicts
        resolutions = {
            str(conflicts[0]["key"]): {"choice": "local"},
            str(conflicts[1]["key"]): {"choice": "remote"},
        }

        result = resolve_documents(base, local, remote, resolutions)

        self.assertFalse(result.success)
        self.assertIn("does not match a complete", result.reason)

    def test_ignores_volatile_reference_index_and_prefers_local_serialization(self):
        base = self.document(["Alpha", "Beta"], type_index=53)
        local = self.document(["Alpha local", "Beta"], type_index=119)
        remote = self.document(["Alpha", "Beta remote"], type_index=140)

        result = merge_documents(base, local, remote)

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertIn(f"119:{REF_UUID}".encode(), result.data)
        self.assertEqual(
            ["Alpha local", "Beta remote"], semantic_values(parse_document(result.data))
        )

    def test_accepts_one_sided_structural_change(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha"])
        remote = self.document(["Alpha", "Beta"])

        result = merge_documents(base, local, remote)

        self.assertTrue(result.success)
        self.assertEqual(remote.data, result.data)

    def test_rejects_two_sided_structural_change(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha", "Local row"])
        remote = self.document(["Remote row", "Alpha"])

        result = merge_documents(base, local, remote)

        self.assertFalse(result.success)
        self.assertEqual("row", result.conflicts[0]["kind"])
        self.assertTrue(all(conflict["requires_choice"] for conflict in result.conflicts))

    def test_does_not_transplant_property_after_row_reordering(self):
        def structured(rows: list[tuple[str, int]], name: str):
            body = "{" + ",".join(
                f'{{{{"#","{label}"}},{value}}}' for label, value in rows
            ) + "}"
            return parse_document(PREFIX + body.encode("utf-8"), name)

        base = structured([("A", 0), ("B", 0)], "base")
        local = structured([("B", 0), ("A", 0)], "local")
        remote = structured([("A", 1), ("B", 0)], "remote")

        result = merge_documents(base, local, remote)

        self.assertFalse(result.success)
        self.assertEqual("row", result.conflicts[0]["kind"])
        self.assertEqual("move", result.conflicts[0]["operation"])

    def test_atomic_write_preserves_existing_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.mxl"
            target.write_bytes(b"original")
            with patch("mxl_tool.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    atomic_write_bytes(target, b"replacement")

            self.assertEqual(b"original", target.read_bytes())
            self.assertEqual([], list(target.parent.glob(".*.tmp")))

    def test_parses_a_large_template_like_document(self):
        data = make_mxl([f"Field {index}" for index in range(400)])
        document = parse_document(data, "large-template.mxl")

        self.assertGreater(len(document.tokens), 1_000)
        self.assertGreater(len(semantic_values(document)), 10)

    def test_merges_non_overlapping_changes_in_a_large_template_like_document(self):
        base_data = make_mxl(
            ["Logistic&Co", "Goods-in-transit"]
            + [f"Template field {index}" for index in range(200)]
        )
        self.assertIn(b"Logistic&Co", base_data)
        self.assertIn(b"Goods-in-transit", base_data)
        local_data = base_data.replace(b"Logistic&Co", b"Logistic&Co local")
        remote_data = base_data.replace(b"Goods-in-transit", b"Goods-in-transit remote")

        result = merge_documents(
            parse_document(base_data, "base"),
            parse_document(local_data, "local"),
            parse_document(remote_data, "remote"),
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertIn(b"Logistic&Co local", result.data)
        self.assertIn(b"Goods-in-transit remote", result.data)
        parse_document(result.data, "merged")

    def test_output_can_be_written_and_parsed_again(self):
        base = self.document(["Alpha", "Beta"])
        local = self.document(["Alpha local", "Beta"])
        remote = self.document(["Alpha", "Beta remote"])
        result = merge_documents(base, local, remote)
        assert result.data is not None

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "merged.mxl"
            output.write_bytes(result.data)
            reparsed = parse_document(output.read_bytes(), str(output))

        self.assertEqual(["Alpha local", "Beta remote"], semantic_values(reparsed))

if __name__ == "__main__":
    unittest.main()
