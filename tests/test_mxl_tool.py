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
    _align_rows,
    _direct_integer_tokens,
    _ensure_attributes_file,
    _independent_move_groups,
    _row_cells,
    _row_layout,
    _row_metadata_sections,
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


def make_row_record_mxl_with_metadata(
    rows: list[tuple[int, str]],
    *,
    properties: list[tuple[int, int]],
    ranges: list[tuple[int, int]],
    grouped_row: int,
    named_row: int,
) -> bytes:
    records = []
    for coordinate, value in rows:
        escaped = value.replace(chr(34), chr(34) * 2)
        records.extend((str(coordinate), "0", "1", "0", f'{{"#","{escaped}"}}'))
    property_values = [
        str(len(properties)),
        *(str(value) for pair in properties for value in pair),
    ]
    range_values = [
        str(len(ranges)),
        *(
            value
            for start, end in ranges
            for value in (f"{{{start},{end},0,{{1,0}},0,0}}", "-1")
        ),
    ]
    tail = [
        "{0}",
        str(max(coordinate for coordinate, _value in rows) + 1),
        "0",
        "{0}",
        "{0}",
        *property_values,
        "0",
        "0",
        *range_values,
        "0",
        "0",
        "0",
        f"{{1,{{1,{grouped_row},3,{grouped_row},0}}}}",
        "{0}",
        "{0}",
        (
            '{1,"Cell",{1,{3,1,'
            f"{named_row},1,{named_row},"
            "00000000-0000-0000-0000-000000000000},0}}"
        ),
    ]
    return PREFIX + (
        "{" + ",".join((str(len(rows)), *records, *tail)) + "}"
    ).encode("utf-8")


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
                    onec_file_editor="1cv8fv.exe",
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
                self.assertEqual(
                    "1cv8fv.exe",
                    subprocess.check_output(
                        ["git", "config", "--get", "mxl.onecFileEditor"],
                        text=True,
                    ).strip(),
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

        # A stale *global* mxl.previewBatchCommand (e.g. from a machine-wide
        # install) must not leak through once this repo has disabled batch
        # mode: either the lookup fails outright, or it resolves to empty.
        self.assertFalse(configured.stdout.decode().strip())
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

    def test_mixed_keep_and_delete_row_decisions_are_composed(self):
        base = parse_document(
            make_row_record_mxl(
                [
                    (0, "Anchor A"),
                    (1, "Conflict"),
                    (2, "Middle"),
                    (3, "Automatic delete"),
                    (4, "Tail"),
                ]
            )
        )
        local = parse_document(
            make_row_record_mxl(
                [
                    (0, "Anchor A"),
                    (1, "Conflict local"),
                    (2, "Middle"),
                    (3, "Automatic delete"),
                    (4, "Tail"),
                ]
            )
        )
        remote = parse_document(
            make_row_record_mxl(
                [(0, "Anchor A"), (2, "Middle"), (4, "Tail")]
            )
        )
        conflicts = merge_documents(base, local, remote).conflicts
        required = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["requires_choice"]
        )
        automatic = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and not conflict["requires_choice"]
        )

        result = resolve_documents(
            base,
            local,
            remote,
            {
                str(required["key"]): {"choice": "local"},
                str(automatic["key"]): {"choice": automatic["default_choice"]},
            },
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertEqual(
            [
                "Anchor A",
                "Conflict local",
                "Middle",
                "Tail",
            ],
            semantic_values(parse_document(result.data)),
        )

    def test_local_keep_can_be_combined_with_remote_move(self):
        base = parse_document(
            make_row_record_mxl(
                [
                    (0, "A"),
                    (1, "B"),
                    (2, "C"),
                    (3, "D"),
                    (4, "E"),
                ]
            )
        )
        local = parse_document(
            make_row_record_mxl(
                [
                    (0, "A"),
                    (1, "B local"),
                    (2, "C"),
                    (3, "D"),
                    (4, "E"),
                ]
            )
        )
        remote = parse_document(
            make_row_record_mxl(
                [(0, "A"), (1, "C"), (2, "E"), (3, "D")]
            )
        )
        conflicts = merge_documents(base, local, remote).conflicts
        required = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["requires_choice"]
        )
        moved = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        )

        result = resolve_documents(
            base,
            local,
            remote,
            {
                str(required["key"]): {"choice": "local"},
                str(moved["key"]): {"choice": "remote"},
            },
        )

        self.assertTrue(result.success)
        assert result.data is not None
        self.assertEqual(
            ["A", "B local", "C", "E", "D"],
            semantic_values(parse_document(result.data)),
        )

    def test_mixed_row_composition_updates_coordinate_metadata(self):
        base = parse_document(
            make_row_record_mxl_with_metadata(
                [(0, "A"), (1, "B"), (2, "C"), (3, "D"), (4, "E")],
                properties=[(1, 5), (2, 7), (4, 9)],
                ranges=[(0, 1), (2, 4)],
                grouped_row=3,
                named_row=4,
            )
        )
        local = parse_document(
            make_row_record_mxl_with_metadata(
                [(0, "A"), (1, "B local"), (2, "C"), (3, "D"), (4, "E")],
                properties=[(1, 5), (2, 7), (4, 9)],
                ranges=[(0, 1), (2, 4)],
                grouped_row=3,
                named_row=4,
            )
        )
        remote = parse_document(
            make_row_record_mxl_with_metadata(
                [(0, "A"), (1, "C"), (2, "E"), (3, "D")],
                properties=[(1, 7), (3, 9)],
                ranges=[(0, 0), (1, 3)],
                grouped_row=2,
                named_row=3,
            )
        )
        conflicts = merge_documents(base, local, remote).conflicts
        required = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["requires_choice"]
        )
        moved = next(
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        )

        result = resolve_documents(
            base,
            local,
            remote,
            {
                str(required["key"]): {"choice": "local"},
                str(moved["key"]): {"choice": "remote"},
            },
        )

        self.assertTrue(result.success, result.reason)
        assert result.data is not None
        merged = parse_document(result.data)
        layout = _row_layout(merged)
        assert layout is not None
        sections = _row_metadata_sections(merged, layout)
        assert sections is not None
        self.assertEqual(
            [(1, 5), (2, 7), (4, 9)],
            sections["properties"],
        )
        merged_ranges = []
        for node in sections["ranges"]:
            indexes = _direct_integer_tokens(merged, node)
            merged_ranges.append(
                (
                    int(merged.tokens[indexes[0]].value),
                    int(merged.tokens[indexes[1]].value),
                )
            )
        self.assertEqual([(0, 1), (2, 4)], merged_ranges)
        grouping = sections["grouping"]
        group = next(
            child for child in grouping.children if not isinstance(child, int)
        )
        group_indexes = _direct_integer_tokens(merged, group)
        self.assertEqual(3, int(merged.tokens[group_indexes[1]].value))
        named = sections["named_areas"]
        named_indexes = [
            index
            for index in range(named.start, named.end)
            if merged.tokens[index].value == "4"
        ]
        self.assertGreaterEqual(len(named_indexes), 2)

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
        self.assertIn("normal MXL row records", result.reason)

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

class IndependentMoveGroupTests(unittest.TestCase):
    def _alignments(self, base_rows, local_rows, remote_rows):
        base = _row_layout(parse_document(make_row_record_mxl(base_rows)))
        local = _row_layout(parse_document(make_row_record_mxl(local_rows)))
        remote = _row_layout(parse_document(make_row_record_mxl(remote_rows)))
        assert base is not None and local is not None and remote is not None
        return {
            "local": _align_rows(base, local),
            "remote": _align_rows(base, remote),
        }

    @staticmethod
    def _rows(values):
        return [(index, value) for index, value in enumerate(values)]

    def test_no_moves_yields_no_groups(self):
        alignments = self._alignments(
            self._rows(["A", "B", "C"]),
            self._rows(["A", "B", "C"]),
            self._rows(["A", "B", "C"]),
        )
        self.assertEqual([], _independent_move_groups(alignments))

    def test_single_move_spans_affected_range(self):
        # Remote lifts D above B: rows 1..3 form one reshuffle region.
        alignments = self._alignments(
            self._rows(["A", "B", "C", "D", "E"]),
            self._rows(["A", "B", "C", "D", "E"]),
            self._rows(["A", "D", "B", "C", "E"]),
        )
        groups = _independent_move_groups(alignments)
        self.assertEqual(1, len(groups))
        self.assertEqual(1, groups[0]["min_base"])
        self.assertEqual(3, groups[0]["max_base"])

    def test_disjoint_moves_on_different_sides_stay_independent(self):
        # Local reorders the head, Remote reorders the tail: two regions.
        alignments = self._alignments(
            self._rows(["A", "B", "C", "D", "E", "F"]),
            self._rows(["B", "A", "C", "D", "E", "F"]),
            self._rows(["A", "B", "C", "D", "F", "E"]),
        )
        groups = _independent_move_groups(alignments)
        self.assertEqual(2, len(groups))
        self.assertEqual((0, 1), (groups[0]["min_base"], groups[0]["max_base"]))
        self.assertEqual((4, 5), (groups[1]["min_base"], groups[1]["max_base"]))

    def test_disjoint_moves_resolve_to_different_sides_at_once(self):
        # Local reorders the head (B before A), Remote reorders the tail
        # (F before E). The regions are disjoint, so each may follow its own
        # side simultaneously — impossible under a single global order.
        base = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "E", "F"]
        )))
        local = parse_document(make_row_record_mxl(self._rows(
            ["B", "A", "C", "D", "E", "F"]
        )))
        remote = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "F", "E"]
        )))
        conflicts = merge_documents(base, local, remote).conflicts
        moves = [
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        ]
        resolutions = {}
        for conflict in moves:
            # Head region rows exist in Local's reorder, tail rows in Remote's.
            side = "local" if conflict["states"]["local"].startswith("moved") else "remote"
            resolutions[str(conflict["key"])] = {"choice": side}

        result = resolve_documents(base, local, remote, resolutions)
        self.assertTrue(result.success, result.reason)
        assert result.data is not None
        self.assertEqual(
            ["B", "A", "C", "D", "F", "E"],
            semantic_values(parse_document(result.data)),
        )

    def test_delete_combined_with_reorder_keeps_moved_values(self):
        # A deletion plus a reorder forces the composer; the value pass must use
        # the composer's exact row map, not re-align by value, or the deleted
        # row's text overwrites a moved cell.
        base = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "E"]
        )))
        local = parse_document(make_row_record_mxl(
            [(0, "A"), (2, "C"), (3, "D"), (4, "E")]
        ))
        remote = parse_document(make_row_record_mxl(
            [(0, "A"), (1, "B"), (2, "E"), (3, "C"), (4, "D")]
        ))
        conflicts = [
            conflict
            for conflict in merge_documents(base, local, remote).conflicts
            if conflict["kind"] == "row"
        ]
        resolutions = {
            str(conflict["key"]): {
                "choice": "local" if conflict["operation"] == "delete" else "remote"
            }
            for conflict in conflicts
        }
        result = resolve_documents(base, local, remote, resolutions)
        self.assertTrue(result.success, result.reason)
        assert result.data is not None
        self.assertEqual(
            ["A", "E", "C", "D"],
            semantic_values(parse_document(result.data)),
        )

    def test_conflicts_tag_moves_with_independent_groups(self):
        base = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "E", "F"]
        )))
        local = parse_document(make_row_record_mxl(self._rows(
            ["B", "A", "C", "D", "E", "F"]
        )))
        remote = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "F", "E"]
        )))
        moves = [
            conflict
            for conflict in merge_documents(base, local, remote).conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        ]
        groups = {conflict["move_group"] for conflict in moves}
        self.assertEqual(2, len(groups))
        self.assertTrue(all(conflict["move_group"] is not None for conflict in moves))

    def test_swapping_first_and_last_rows_is_a_move_not_two_edits(self):
        # SequenceMatcher reports an endpoint swap as two one-for-one "replace"
        # opcodes, which look exactly like ordinary edits. Pairing those
        # positionally used to hide the move: the merge reported a single value
        # conflict, applied the other end silently, and resolving it to anything
        # but the reordering side dropped a row's content entirely
        # (A B C D / D B C A / A B C D! resolved to Remote produced D B C D!).
        base = parse_document(make_row_record_mxl(self._rows(["A", "B", "C", "D"])))
        local = parse_document(make_row_record_mxl(self._rows(["D", "B", "C", "A"])))
        remote = parse_document(make_row_record_mxl(self._rows(["A", "B", "C", "D!"])))

        conflicts = merge_documents(base, local, remote).conflicts
        moves = [
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        ]
        self.assertTrue(moves, "endpoint swap was not reported as a move")

        resolutions = {}
        for conflict in conflicts:
            key = (
                str(conflict["token_index"])
                if conflict["kind"] == "value"
                else str(conflict["key"])
            )
            resolutions[key] = {"choice": "remote"}
        result = resolve_documents(base, local, remote, resolutions)
        self.assertTrue(result.success, result.reason)
        assert result.data is not None
        values = semantic_values(parse_document(result.data))
        # Whatever order the choice produces, no row may vanish or be duplicated.
        self.assertEqual(sorted(values), sorted(["A", "B", "C", "D!"]))

    def test_move_region_absorbs_in_span_additions_and_deletions(self):
        # Real three-way sample where Remote reordered a block while both sides
        # also added and removed rows inside that span. The region must gather
        # the reorder together with the in-span adds/deletes and require an
        # explicit choice; resolving the whole file to Local must then reproduce
        # Local's exact row structure rather than a corrupted mixed layout.
        merge_dir = Path(__file__).parent / "merge"
        base = parse_document((merge_dir / "base.mxl").read_bytes(), "base")
        local = parse_document((merge_dir / "local.mxl").read_bytes(), "local")
        remote = parse_document((merge_dir / "remote.mxl").read_bytes(), "remote")
        conflicts = merge_documents(base, local, remote).conflicts

        region_ops = [
            conflict
            for conflict in conflicts
            if conflict["kind"] == "row" and conflict.get("move_group") == "movegroup:0"
        ]
        operations = {conflict["operation"] for conflict in region_ops}
        self.assertIn("move", operations)
        self.assertTrue({"add", "delete"} & operations)
        self.assertTrue(all(conflict["requires_choice"] for conflict in region_ops))

        resolutions = {}
        for conflict in conflicts:
            key = (
                str(conflict["token_index"])
                if conflict["kind"] == "value"
                else str(conflict["key"])
            )
            if conflict.get("requires_choice", True):
                resolutions[key] = {"choice": "local"}
            elif conflict.get("default_choice"):
                resolutions[key] = {"choice": conflict["default_choice"]}
        result = resolve_documents(base, local, remote, resolutions)
        self.assertTrue(result.success, result.reason)
        assert result.data is not None

        merged = parse_document(result.data)
        merged_layout = _row_layout(merged)
        local_layout = _row_layout(local)
        assert merged_layout is not None and local_layout is not None

        def shape(document, layout):
            return [len(_row_cells(document, row)) for row in layout.rows]

        # Same row count and per-row cell structure as Local (value edits aside),
        # i.e. the composed layout is not the corrupted, row-shifted mix.
        self.assertEqual(shape(merged, merged_layout), shape(local, local_layout))

    def test_structural_region_keeps_blocks_intact_when_mixed(self):
        # Region ordered from Remote while an unrelated row (18) is kept from
        # Local forces the composer. The reordered span must arrive as Remote's
        # whole sub-layout — its title / spacer / header / data blocks intact —
        # not as individually shuffled rows that drop the spacer between blocks.
        merge_dir = Path(__file__).parent / "merge"
        base = parse_document((merge_dir / "base.mxl").read_bytes(), "base")
        local = parse_document((merge_dir / "local.mxl").read_bytes(), "local")
        remote = parse_document((merge_dir / "remote.mxl").read_bytes(), "remote")
        conflicts = merge_documents(base, local, remote).conflicts

        resolutions = {}
        for conflict in conflicts:
            key = (
                str(conflict["token_index"])
                if conflict["kind"] == "value"
                else str(conflict["key"])
            )
            if conflict.get("move_group") == "movegroup:0":
                resolutions[key] = {"choice": "remote"}
            elif conflict.get("requires_choice", True):
                resolutions[key] = {"choice": "local"}
            elif conflict.get("default_choice"):
                resolutions[key] = {"choice": conflict["default_choice"]}
        result = resolve_documents(base, local, remote, resolutions)
        self.assertTrue(result.success, result.reason)
        assert result.data is not None

        merged = parse_document(result.data)
        layout = _row_layout(merged)
        assert layout is not None
        counts = [len(_row_cells(merged, row)) for row in layout.rows]
        values = [row.values for row in layout.rows]
        # A one-cell section title must never sit directly above a wide header
        # row: that adjacency is the torn-block corruption 1C rendered broken.
        for index in range(len(counts) - 1):
            title = counts[index] == 1 and any(values[index])
            wide_header = counts[index + 1] >= 3
            self.assertFalse(
                title and wide_header,
                f"row {layout.rows[index].coordinate} lost its spacer",
            )

    def test_overlapping_two_sided_moves_require_explicit_choice(self):
        # Local reorders 1..3, Remote reorders 2..4: the merged region cannot be
        # ordered from one side automatically, so every move must require a
        # choice rather than silently applying contradictory one-sided defaults.
        base = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "C", "D", "E", "F"]
        )))
        local = parse_document(make_row_record_mxl(self._rows(
            ["A", "C", "D", "B", "E", "F"]
        )))
        remote = parse_document(make_row_record_mxl(self._rows(
            ["A", "B", "D", "E", "C", "F"]
        )))
        moves = [
            conflict
            for conflict in merge_documents(base, local, remote).conflicts
            if conflict["kind"] == "row" and conflict["operation"] == "move"
        ]
        self.assertTrue(moves)
        self.assertEqual({conflict["move_group"] for conflict in moves}, {"movegroup:0"})
        self.assertTrue(all(conflict["requires_choice"] for conflict in moves))
        self.assertTrue(all(conflict["default_choice"] is None for conflict in moves))

    def test_overlapping_moves_across_sides_merge_into_one_group(self):
        # Local reorders 1..3, Remote reorders 2..4: ranges overlap -> merged.
        alignments = self._alignments(
            self._rows(["A", "B", "C", "D", "E", "F"]),
            self._rows(["A", "C", "D", "B", "E", "F"]),
            self._rows(["A", "B", "D", "E", "C", "F"]),
        )
        groups = _independent_move_groups(alignments)
        self.assertEqual(1, len(groups))
        self.assertEqual(1, groups[0]["min_base"])
        self.assertEqual(4, groups[0]["max_base"])


if __name__ == "__main__":
    unittest.main()
