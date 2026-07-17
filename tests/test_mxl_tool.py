from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mxl_tool import (
    MAGIC,
    MXL_ATTRIBUTES_LINE,
    UTF8_BOM,
    _ensure_attributes_file,
    merge_documents,
    parse_document,
    resolve_documents,
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
        self.assertIn("Alpha", textconv(document))

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

    def test_merges_non_overlapping_value_changes(self):
        base = self.document(["Alpha", "Beta"], name="base")
        local = self.document(["Alpha local", "Beta"], name="local")
        remote = self.document(["Alpha", "Beta remote"], name="remote")

        result = merge_documents(base, local, remote)

        self.assertTrue(result.success)
        assert result.data is not None
        merged = parse_document(result.data)
        self.assertEqual(["Alpha local", "Beta remote"], semantic_values(merged))

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

    def test_resolves_structural_conflict_by_selecting_complete_document(self):
        base = self.document(["Alpha"])
        local = self.document(["Alpha", "Local row"])
        remote = self.document(["Remote row", "Alpha"])

        result = resolve_documents(
            base, local, remote, {"structural": {"choice": "remote"}}
        )

        self.assertTrue(result.success)
        self.assertEqual(remote.data, result.data)

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
        self.assertEqual("structural", result.conflicts[0]["kind"])

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
