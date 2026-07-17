#!/usr/bin/env python3
"""Local web UI for resolving semantic 1C MXL merge conflicts."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    from tools.mxl_merge.mxl_preview import (
        PreviewBundle,
        build_preview_bundle,
        configured_batch_preview_command,
        configured_preview_command,
        render_document_html_batch,
        render_document_html,
    )
except ModuleNotFoundError:
    from mxl_preview import (  # type: ignore[no-redef]
        PreviewBundle,
        build_preview_bundle,
        configured_batch_preview_command,
        configured_preview_command,
        render_document_html_batch,
        render_document_html,
    )

try:
    from tools.mxl_merge.mxl_tool import (
        MergeResult,
        MxlDocument,
        MxlFormatError,
        load_document,
        merge_documents,
        parse_document,
        resolve_documents,
        semantic_entries,
    )
except ModuleNotFoundError:
    from mxl_tool import (  # type: ignore[no-redef]
        MergeResult,
        MxlDocument,
        MxlFormatError,
        load_document,
        merge_documents,
        parse_document,
        resolve_documents,
        semantic_entries,
    )


MAX_REQUEST_SIZE = 2 * 1024 * 1024
SCRIPT_TAG_RE = re.compile(br"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)


def _sanitize_preview_html(data: bytes) -> bytes:
    """Keep renderer output static so preview frames never execute embedded code."""
    return SCRIPT_TAG_RE.sub(b"", data)


@dataclass
class UiSession:
    base: MxlDocument
    local: MxlDocument
    remote: MxlDocument
    output_path: Path
    preview_bundle: PreviewBundle | None = None
    preview_command: str | None = None
    batch_preview_command: str | None = None

    @classmethod
    def from_paths(
        cls, base_path: str, local_path: str, remote_path: str, output_path: str
    ) -> "UiSession":
        return cls(
            load_document(base_path),
            load_document(local_path),
            load_document(remote_path),
            Path(output_path),
        )

    def initial_result(self) -> MergeResult:
        return merge_documents(self.base, self.local, self.remote)

    def prepare_previews(
        self,
        preview_command: str | None = None,
        batch_preview_command: str | None = None,
    ) -> None:
        self.preview_command = configured_preview_command(preview_command)
        # An explicit one-document converter is a complete per-run override.
        # Pass --preview-batch-command as well when both explicit converters
        # should be used; otherwise do not mix it with a repository setting.
        self.batch_preview_command = (
            ""
            if preview_command is not None and batch_preview_command is None
            else configured_batch_preview_command(batch_preview_command)
        )
        self.preview_bundle = build_preview_bundle(
            {"base": self.base, "local": self.local, "remote": self.remote},
            self.preview_command,
            self.batch_preview_command,
        )

    def model(self) -> dict[str, Any]:
        result = self.initial_result()
        if self.preview_bundle is None:
            self.prepare_previews()
        assert self.preview_bundle is not None
        preview = self.preview_bundle

        semantic_rows = [dict(row) for row in preview.semantic.rows]
        base_entries = semantic_entries(self.base)
        conflict_keys = {
            str(conflict["token_index"])
            for conflict in result.conflicts
            if conflict["kind"] == "value"
        }
        field_by_conflict: dict[str, str] = {}
        for row_index, row in enumerate(semantic_rows):
            field_id = f"field-{row_index}"
            row["id"] = field_id
            anchor = row.get("anchor")
            if row.get("base") is not None and isinstance(anchor, int) and anchor < len(base_entries):
                conflict_key = str(base_entries[anchor][0])
                if conflict_key in conflict_keys:
                    row["conflict_key"] = conflict_key
                    field_by_conflict[conflict_key] = field_id

        conflicts: list[dict[str, Any]] = []
        for conflict in result.conflicts:
            item = dict(conflict)
            item["key"] = (
                "structural"
                if conflict["kind"] == "structural"
                else str(conflict["token_index"])
            )
            item["manual_allowed"] = conflict.get("token_type") in {"string", "atom"}
            if item["key"] in field_by_conflict:
                item["field_id"] = field_by_conflict[item["key"]]
            conflicts.append(item)

        return {
            "status": "ready" if result.success else "conflict",
            "reason": result.reason,
            "paths": {
                "base": self.base.path,
                "local": self.local.path,
                "remote": self.remote.path,
                "output": str(self.output_path),
            },
            "conflicts": conflicts,
            "previews": {
                "semantic": {
                    "rows": semantic_rows,
                    "total": preview.semantic.total_rows,
                    "truncated": preview.semantic.truncated,
                    "stats": preview.semantic.stats,
                },
                "rendered": {
                    "provider": preview.renderer,
                    "available": list(preview.rendered_html),
                    "errors": preview.errors,
                },
            },
        }

    def render_result_preview(
        self, resolutions: Mapping[str, Mapping[str, object]]
    ) -> tuple[MergeResult, bytes | None]:
        result = resolve_documents(self.base, self.local, self.remote, resolutions)
        if not result.success or result.data is None:
            return result, None
        if not self.preview_command and not self.batch_preview_command:
            return result, None
        document = parse_document(result.data, "<merged-preview>")
        if self.batch_preview_command:
            return result, render_document_html_batch(
                document, self.batch_preview_command
            )
        assert self.preview_command is not None
        return result, render_document_html(document, self.preview_command)

    def resolve(self, resolutions: Mapping[str, Mapping[str, object]]) -> MergeResult:
        result = resolve_documents(
            self.base, self.local, self.remote, resolutions
        )
        if not result.success or result.data is None:
            return result

        # Parse before and after writing so the UI never reports success for a
        # malformed serialization and Git never receives a corrupt output.
        parse_document(result.data, str(self.output_path))
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(result.data)
        return result


class MxlUiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        session: UiSession,
        token: str,
        html: bytes,
    ) -> None:
        super().__init__(server_address, MxlUiRequestHandler)
        self.session = session
        self.token = token
        self.html = html
        self.saved = False
        self.cancelled = False
        self.result_html: bytes | None = None
        self.result_revision = 0


class MxlUiRequestHandler(BaseHTTPRequestHandler):
    server: MxlUiServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized_api_path(self, action: str) -> bool:
        return urlparse(self.path).path == f"/api/{self.server.token}/{action}"

    def _shutdown_after_response(self) -> None:
        """Let the browser read the JSON response before closing the server."""
        self.wfile.flush()
        timer = threading.Timer(0.25, self.server.shutdown)
        timer.daemon = True
        timer.start()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        preview_prefix = f"/preview/{self.server.token}/"
        if path.startswith(preview_prefix):
            side = path.removeprefix(preview_prefix)
            if side == "result":
                data = self.server.result_html
            else:
                bundle = self.server.session.preview_bundle
                data = bundle.rendered_html.get(side) if bundle is not None else None
            if data is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = _sanitize_preview_html(data)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            )
            self.end_headers()
            self.wfile.write(data)
            return

        if path != f"/session/{self.server.token}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.server.html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; frame-src 'self'",
        )
        self.end_headers()
        self.wfile.write(self.server.html)

    def _read_payload(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None
        if length <= 0 or length > MAX_REQUEST_SIZE:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Invalid request size"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON payload"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected a JSON object"})
            return None
        return payload

    def do_POST(self) -> None:
        if self._authorized_api_path("cancel"):
            self.server.cancelled = True
            self._send_json(HTTPStatus.OK, {"status": "cancelled"})
            self._shutdown_after_response()
            return

        is_result_preview = self._authorized_api_path("preview-result")
        if not is_result_preview and not self._authorized_api_path("resolve"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        payload = self._read_payload()
        if payload is None:
            return
        resolutions = payload.get("resolutions", {})
        if not isinstance(resolutions, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid resolutions"})
            return

        if is_result_preview:
            try:
                result, html = self.server.session.render_result_preview(resolutions)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if not result.success:
                self._send_json(HTTPStatus.CONFLICT, {"error": result.reason})
                return
            if html is None:
                self._send_json(
                    HTTPStatus.OK,
                    {"status": "semantic-only", "reason": result.reason},
                )
                return
            self.server.result_html = html
            self.server.result_revision += 1
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "rendered",
                    "reason": result.reason,
                    "revision": self.server.result_revision,
                },
            )
            return

        try:
            result = self.server.session.resolve(resolutions)
        except (OSError, MxlFormatError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        if not result.success:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": result.reason,
                    "conflicts": result.conflicts,
                },
            )
            return

        self.server.saved = True
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "saved",
                "reason": result.reason,
                "output": str(self.server.session.output_path),
            },
        )
        self._shutdown_after_response()


def _safe_json_for_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_ui(model: Mapping[str, object], token: str) -> bytes:
    template_path = Path(__file__).with_name("ui.html")
    template = template_path.read_text(encoding="utf-8")
    html = template.replace("__MXL_MODEL__", _safe_json_for_script(model))
    html = html.replace("__MXL_TOKEN__", _safe_json_for_script(token))
    return html.encode("utf-8")


def create_ui_server(
    session: UiSession,
    host: str = "127.0.0.1",
    port: int = 0,
    preview_command: str | None = None,
    batch_preview_command: str | None = None,
) -> tuple[MxlUiServer, str]:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The MXL merge UI can only bind to a loopback address")
    session.prepare_previews(preview_command, batch_preview_command)
    token = secrets.token_urlsafe(24)
    server = MxlUiServer(
        (host, port), session, token, render_ui(session.model(), token)
    )
    actual_host, actual_port = server.server_address[:2]
    url_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    return server, f"http://{url_host}:{actual_port}/session/{token}"


def run_ui(
    base_path: str,
    local_path: str,
    remote_path: str,
    output_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    preview_command: str | None = None,
    batch_preview_command: str | None = None,
) -> int:
    try:
        session = UiSession.from_paths(base_path, local_path, remote_path, output_path)
        server, url = create_ui_server(
            session, host, port, preview_command, batch_preview_command
        )
    except (OSError, MxlFormatError, ValueError) as error:
        print(f"mxl-ui: {error}")
        return 2

    print(f"MXL merge UI: {url}")
    print("Choose conflict resolutions in the browser. Press Ctrl+C to cancel.")
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.cancelled = True
    finally:
        server.server_close()

    return 0 if server.saved else 1
