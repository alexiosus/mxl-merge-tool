from __future__ import annotations

import json
import shlex
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from mxl_tool import parse_document, semantic_values
from mxl_ui import (
    UiSession,
    _sanitize_preview_html,
    create_ui_server,
    render_ui,
)
from tests.test_mxl_tool import make_mxl


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
