from __future__ import annotations

import json
import shlex
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from mxl_tool import parse_document, semantic_values
from mxl_ui import (
    UiSession,
    _sanitize_preview_html,
    create_ui_server,
    render_ui,
)
from tests.test_mxl_tool import make_mxl, make_row_record_mxl


class MxlUiTests(unittest.TestCase):
    def test_rendered_ui_contains_model_without_interpreting_embedded_html(self):
        model = {
            "status": "conflict",
            "reason": "Test",
            "paths": {},
            "conflicts": [],
            "previews": {
                "semantic": {"rows": [], "total": 0, "truncated": False, "stats": {}},
                "rendered": {"provider": None, "available": [], "errors": {}},
            },
            "unsafe": "</script><script>alert(1)</script>",
        }

        html = render_ui(model, "token").decode("utf-8")

        self.assertIn("MXL Merge Resolver", html)
        self.assertIn('id="documentTitle"', html)
        self.assertIn("model.paths.output", html)
        self.assertNotIn('class="paths-popover"', html)
        self.assertIn("Merged result", html)
        self.assertIn('data-view-mode="changes"', html)
        self.assertIn("preview-result", html)
        self.assertIn("mxl-unresolved", html)
        self.assertIn("Manual value", html)
        self.assertIn("previousConflict", html)
        self.assertIn('id="dimToggle"', html)
        self.assertIn('id="zoomReset"', html)
        self.assertIn("function scrollFrameElementIntoView", html)
        self.assertIn("frame-coordinate-columns", html)
        self.assertIn("frame-coordinate-rows", html)
        self.assertNotIn("function excelColumnName", html)
        self.assertIn("label.textContent = String(segment.index)", html)
        self.assertIn("function installFrameCoordinateRulers", html)
        self.assertIn("const referenceRow = [...geometryByRow.entries()]", html)
        self.assertIn('const rowElement = anchor.element.closest("tr") || anchor.element', html)
        self.assertIn("frame._mxlCoordinateDocument !== frameDocument", html)
        self.assertIn('frameDocument.addEventListener("wheel", refreshAfterInput', html)
        self.assertIn("function setElementChip", html)
        self.assertIn("function resolutionForRow", html)
        self.assertIn('element.dataset.mxlChip = choice.toUpperCase()', html)
        self.assertIn('data-mxl-chip-choice="local"', html)
        self.assertIn("const placeholderBands", html)
        self.assertIn("marker.dataset.mxlRowCoordinate = rowCoordinate", html)
        self.assertIn('label.textContent = band.state === "unresolved" ? "?" : "×"', html)
        self.assertIn(
            'frameWindow.addEventListener("scroll", refreshRulers',
            html,
        )
        self.assertIn("sourceScrollSyncSuspended", html)
        self.assertIn("sourceFocusTargets.forEach(scrollFrameElementIntoView)", html)
        self.assertIn("function focusPendingResultConflict", html)
        self.assertIn("pendingResultFocusKey = key", html)
        self.assertIn(
            "} else if (wholeDocumentChoice() || !conflictByKey.structural) {",
            html,
        )
        self.assertNotIn(
            '!conflictByKey.structural && selectedRowStructureSource() !== ""',
            html,
        )
        self.assertIn('choose(row.conflict_key, side, "", true)', html)
        self.assertIn("focusResult = false", html)
        self.assertIn(
            'querySelectorAll("td, th, div, span, p, font")', html
        )
        self.assertIn("function renderRowConflictPlaceholders", html)
        self.assertIn("mxl-row-placeholder", html)
        self.assertIn("mxl-row-provisional-hidden", html)
        self.assertIn("mxl-row-conflict-line", html)
        self.assertIn("function conflictVisualElement", html)
        self.assertIn("const requiresManualChoice", html)
        self.assertIn("function draftRowStructureSource", html)
        self.assertIn("draftRowStructurePreference = choice", html)
        self.assertIn(
            "const source = documentChoice || structuralChoice || draftSource",
            html,
        )
        self.assertIn("function chooseWholeDocument", html)
        self.assertIn("function fillUnresolved", html)
        self.assertIn('id="undoButton"', html)
        self.assertIn('id="redoButton"', html)
        self.assertIn("function captureDecisionState", html)
        self.assertIn("function commitDecisionHistory", html)
        self.assertIn("function undoDecision", html)
        self.assertIn("function redoDecision", html)
        self.assertIn("function bindManualHistory", html)
        self.assertIn("function chooseAll", html)
        self.assertIn('const historyGroup = `manual:${key}`', html)
        self.assertIn('if (event.shiftKey) redoDecision()', html)
        self.assertIn("resolutions.document = {choice: side}", html)
        self.assertIn("data-whole-document", html)
        self.assertIn("Resolve pending", html)
        self.assertIn(
            "the selected row combination is incompatible and cannot be saved",
            html,
        )
        self.assertIn("unresolved row conflicts are placeholders", html)
        self.assertIn("Automatic row change", html)
        self.assertIn("function coordinateForRow", html)
        self.assertIn("Selected cell · ${selectedCoordinate", html)
        self.assertIn("mxlCoordinate", html)
        self.assertIn("data-mxl-chip", html)
        self.assertIn("semantic-coordinate", html)
        self.assertIn("structural-row-change", html)
        self.assertIn("unresolved — choose Base, Local, or Remote", html)
        self.assertNotIn("doc.body.prepend(marker)", html)
        self.assertIn("function draftResolvedRowValue", html)
        self.assertNotIn("mxl-row-conflict-badge", html)
        self.assertIn("Showing the ${draftSource[0].toUpperCase()}", html)
        self.assertNotIn(
            "Resolve every row operation to preview the resulting row structure", html
        )
        self.assertIn("scroller.scrollTop", html)
        self.assertNotIn("view.scrollTo", html)
        self.assertNotIn("resultElement?.scrollIntoView", html)
        self.assertIn("border: 2px solid transparent", html)
        self.assertIn("height: calc(100dvh - var(--topbar-height) - 16px)", html)
        self.assertIn("grid-template-rows: repeat(2, minmax(0, 1fr))", html)
        self.assertIn('sandbox="allow-same-origin"', html)
        self.assertNotIn('sandbox="allow-same-origin allow-scripts"', html)
        self.assertIn("resetDecisions", html)
        self.assertIn("cancelButton.hidden = true", html)
        self.assertIn("if (sessionEnded) return", html)
        self.assertIn("picker-manual.selected", html)
        self.assertIn("active-conflict-card", html)
        self.assertIn("Render exact", html)
        self.assertNotIn('class="logo"', html)
        self.assertNotIn('id="progressBar"', html)
        self.assertNotIn('id="reason"', html)
        self.assertNotIn('id="providerBadge"', html)
        self.assertNotIn("Exact merged MXL", html)
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn("\\u003c/script", html)

    def test_preview_html_is_static_and_drops_scripts(self):
        source = b"<html><body><table><tr><td>Value</td></tr></table><script>alert(1)</script></body></html>"

        sanitized = _sanitize_preview_html(source)

        self.assertIn(b"<table>", sanitized)
        self.assertNotIn(b"<script", sanitized.lower())
        self.assertNotIn(b"alert(1)", sanitized)

    def test_external_previews_are_deferred_from_ui_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for side, value in (("base", "A"), ("local", "L"), ("remote", "R")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                paths[side] = path
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(root / "merged.mxl"),
            )
            session.prepare_previews("converter {input} {output}", defer_external=True)
            semantic_bundle = session.preview_bundle

            def slow_build(*args, **kwargs):
                time.sleep(0.15)
                return semantic_bundle

            with patch("mxl_ui.build_preview_bundle", side_effect=slow_build):
                session.start_deferred_previews()
            self.assertTrue(session.preview_loading)
            deadline = time.monotonic() + 1
            while session.preview_loading and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(session.preview_loading)

    def test_model_exposes_individual_row_operation_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            values = {
                "base": ["Alpha", "Beta"],
                "local": ["Alpha", "Inserted", "Beta"],
                "remote": ["Alpha", "Beta remote"],
            }
            for side, side_values in values.items():
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl(side_values))
                paths[side] = path
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(root / "merged.mxl"),
            )

            model = session.model()

        row_conflict = next(
            conflict for conflict in model["conflicts"] if conflict["kind"] == "row"
        )
        self.assertTrue(row_conflict["key"].startswith("row:add:"))
        self.assertEqual("add", row_conflict["operation"])
        self.assertEqual("absent", row_conflict["states"]["base"])
        self.assertEqual("present", row_conflict["states"]["local"])
        self.assertEqual("local", row_conflict["default_choice"])
        self.assertFalse(row_conflict["requires_choice"])

    def test_model_exposes_automatic_cell_change_as_overrideable_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for side, value in (
                ("base", "Alpha"),
                ("local", "Alpha local"),
                ("remote", "Alpha"),
            ):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                paths[side] = path
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(root / "merged.mxl"),
            )

            model = session.model()

        decision = next(
            conflict
            for conflict in model["conflicts"]
            if conflict.get("automatic") and conflict["kind"] == "value"
        )
        row = model["previews"]["semantic"]["rows"][0]
        self.assertEqual("local", decision["default_choice"])
        self.assertFalse(decision["requires_choice"])
        self.assertTrue(decision["manual_allowed"])
        self.assertEqual(decision["key"], row["conflict_key"])
        self.assertEqual(["field-0"], decision["field_ids"])
        self.assertEqual(
            {"base": ["R1C1"], "local": ["R1C1"], "remote": ["R1C1"]},
            decision["coordinates"],
        )

    def test_deleted_edited_row_is_linked_to_preview_and_requires_a_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {
                "base": [(0, "Alpha"), (1, "Beta"), (2, "Gamma")],
                "local": [(0, "Alpha"), (1, "Beta local"), (2, "Gamma")],
                "remote": [(0, "Alpha"), (2, "Gamma")],
            }
            paths = {}
            for side, rows in documents.items():
                path = root / f"{side}.mxl"
                path.write_bytes(make_row_record_mxl(rows))
                paths[side] = path
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(root / "merged.mxl"),
            )

            model = session.model()

        row_conflict = next(
            conflict for conflict in model["conflicts"] if conflict["kind"] == "row"
        )
        linked_rows = [
            row
            for row in model["previews"]["semantic"]["rows"]
            if row.get("row_conflict_key") == row_conflict["key"]
        ]
        self.assertIsNone(row_conflict["default_choice"])
        self.assertTrue(row_conflict["requires_choice"])
        self.assertEqual(2, row_conflict["row_number"])
        self.assertTrue(row_conflict["field_ids"])
        self.assertEqual(1, len(linked_rows))
        self.assertEqual("Beta", linked_rows[0]["base"])
        self.assertEqual("Beta local", linked_rows[0]["local"])
        self.assertIsNone(linked_rows[0]["remote"])
        self.assertEqual(
            {"base": "R2C1", "local": "R2C1", "remote": None},
            linked_rows[0]["coordinates"],
        )
        self.assertEqual(
            {"base": 2, "local": 2, "remote": None},
            row_conflict["row_coordinates"],
        )

    def test_http_ui_saves_selected_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.mxl"
            local = root / "local.mxl"
            remote = root / "remote.mxl"
            output = root / "merged.mxl"
            base.write_bytes(make_mxl(["Alpha"]))
            local.write_bytes(make_mxl(["Local"]))
            remote.write_bytes(make_mxl(["Remote"]))
            session = UiSession.from_paths(
                str(base), str(local), str(remote), str(output)
            )
            model = session.model()
            conflict_key = model["conflicts"][0]["key"]
            self.assertEqual(conflict_key, model["previews"]["semantic"]["rows"][0]["conflict_key"])
            self.assertEqual("field-0", model["conflicts"][0]["field_id"])
            server, url = create_ui_server(session)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                with urllib.request.urlopen(url) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("MXL Merge Resolver", html)

                token = url.rsplit("/", 1)[-1]
                payload = json.dumps(
                    {"resolutions": {conflict_key: {"choice": "remote"}}}
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"{url.split('/session/', 1)[0]}/api/{token}/resolve",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.loads(response.read())
                thread.join(timeout=3)
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual("saved", result["status"])
            self.assertFalse(thread.is_alive())
            document = parse_document(output.read_bytes(), str(output))
            self.assertEqual(["Remote"], semantic_values(document))

    def test_http_ui_cancel_returns_json_before_server_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for side, value in (("base", "Alpha"), ("local", "Local"), ("remote", "Remote")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                paths[side] = path
            output = root / "merged.mxl"
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(output),
            )
            server, url = create_ui_server(session)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                token = url.rsplit("/", 1)[-1]
                request = urllib.request.Request(
                    f"{url.split('/session/', 1)[0]}/api/{token}/cancel",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.loads(response.read())
                thread.join(timeout=3)
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual("cancelled", result["status"])
            self.assertFalse(thread.is_alive())
            self.assertFalse(output.exists())

    def test_renders_exact_preview_for_resolved_in_memory_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for side, value in (("base", "Alpha"), ("local", "Local"), ("remote", "Remote")):
                path = root / f"{side}.mxl"
                path.write_bytes(make_mxl([value]))
                paths[side] = path
            converter = root / "converter.py"
            converter.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "source, target = map(Path, sys.argv[1:3])\n"
                "target.write_text(source.read_bytes().decode('utf-8', errors='ignore'), encoding='utf-8')\n",
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
            session = UiSession.from_paths(
                str(paths["base"]),
                str(paths["local"]),
                str(paths["remote"]),
                str(root / "merged.mxl"),
            )
            session.prepare_previews(command)
            key = session.model()["conflicts"][0]["key"]

            result, html = session.render_result_preview({key: {"choice": "remote"}})

        self.assertTrue(result.success)
        self.assertIsNotNone(html)
        self.assertIn(b"Remote", html)


if __name__ == "__main__":
    unittest.main()
