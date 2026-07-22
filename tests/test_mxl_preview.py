from __future__ import annotations

import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mxl_preview import (
    align_semantic_values,
    build_preview_bundle,
    render_document_html_batch,
    render_document_html,
)
from mxl_tool import load_document
from tests.test_mxl_tool import make_mxl


class MxlPreviewTests(unittest.TestCase):
    def test_aligns_insertions_and_deletions_against_base(self):
        alignment = align_semantic_values(
            ["A", "B", "C"],
            ["A", "Inserted", "B", "C"],
            ["A", "C"],
        )

        self.assertEqual(4, alignment.total_rows)
        self.assertTrue(
            any(
                row["base"] is None and row["local"] == "Inserted"
                for row in alignment.rows
            )
        )
        self.assertTrue(
            any(row["base"] == "B" and row["remote"] is None for row in alignment.rows)
        )
        deleted = next(row for row in alignment.rows if row["base"] == "B")
        self.assertEqual({"base": 1, "local": 2, "remote": None}, deleted["indices"])

    def test_visualizes_reordering_as_structural_changes(self):
        alignment = align_semantic_values(
            ["A", "B", "C"],
            ["B", "A", "C"],
            ["A", "B", "C"],
        )

        changed = alignment.total_rows - alignment.stats.get("unchanged", 0)
        self.assertGreater(changed, 0)
        self.assertTrue(any(row["base"] is None for row in alignment.rows))

    def test_large_repetitive_alignment_uses_bounded_time(self):
        values = ["Repeated"] * 5_000
        started = time.monotonic()

        alignment = align_semantic_values(values, values[:-1] + ["Changed"], values)

        self.assertEqual(5_000, alignment.total_rows)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_external_renderer_produces_html_for_all_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {}
            for side, value in (("base", "A"), ("local", "L"), ("remote", "R")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                documents[side] = load_document(path)

            converter = root / "converter.py"
            converter.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "source, target = map(Path, sys.argv[1:3])\n"
                "target.write_text('<html><body>' + source.name + '</body></html>', encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(str(converter)),
                    "{input}",
                    "{output}",
                )
            )

            bundle = build_preview_bundle(documents, command)

        self.assertEqual({"base", "local", "remote"}, set(bundle.rendered_html))
        self.assertFalse(bundle.errors)
        self.assertIn(b"base.mxl", bundle.rendered_html["base"])

    def test_single_document_renderers_run_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {}
            for side, value in (("base", "A"), ("local", "L"), ("remote", "R")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                documents[side] = load_document(path)

            active = 0
            peak_active = 0
            active_lock = threading.Lock()

            def slow_render(document, command, side, output_directory):
                nonlocal active, peak_active
                with active_lock:
                    active += 1
                    peak_active = max(peak_active, active)
                time.sleep(0.05)
                with active_lock:
                    active -= 1
                return side.encode("utf-8")

            with patch("mxl_preview._render_with_command", side_effect=slow_render):
                bundle = build_preview_bundle(documents, "converter {input} {output}")

        self.assertEqual({"base", "local", "remote"}, set(bundle.rendered_html))
        self.assertGreaterEqual(peak_active, 2)

    def test_external_renderer_can_render_in_memory_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mxl"
            source.write_bytes(make_mxl(["Merged value"]))
            document = load_document(source)
            source.unlink()
            converter = root / "converter.py"
            converter.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "source, target = map(Path, sys.argv[1:3])\n"
                "target.write_text(str(source.exists()) + ':' + str(source.stat().st_size), encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(str(converter)),
                    "{input}",
                    "{output}",
                )
            )

            html = render_document_html(document, command)

        self.assertTrue(html.startswith(b"True:"))

    def test_batch_renderer_processes_all_sides_in_one_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {}
            for side, value in (("base", "A"), ("local", "L"), ("remote", "R")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                documents[side] = load_document(path)
            counter = root / "counter.txt"
            converter = root / "batch_converter.py"
            converter.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "counter.write_text('1', encoding='utf-8')\n"
                "manifest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
                "for item in manifest['items']:\n"
                "    Path(item['outputPath']).write_text(item['name'], encoding='utf-8')\n",
                encoding="utf-8",
            )
            batch_command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(str(converter)),
                    "{manifest}",
                )
            )

            bundle = build_preview_bundle(
                documents,
                batch_preview_command=batch_command,
            )

            self.assertEqual("1", counter.read_text(encoding="utf-8"))

        self.assertEqual("external-batch", bundle.renderer)
        self.assertEqual({"base", "local", "remote"}, set(bundle.rendered_html))

    def test_batch_renderer_can_render_in_memory_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mxl"
            source.write_bytes(make_mxl(["Merged value"]))
            document = load_document(source)
            converter = root / "batch_converter.py"
            converter.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "manifest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
                "item = manifest['items'][0]\n"
                "Path(item['outputPath']).write_text(item['name'], encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = " ".join(
                (
                    shlex.quote(sys.executable),
                    shlex.quote(str(converter)),
                    "{manifest}",
                )
            )

            html = render_document_html_batch(document, command)

        self.assertEqual(b"result", html)


if __name__ == "__main__":
    unittest.main()
