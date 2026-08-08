#!/usr/bin/env python3
"""Local web UI for resolving semantic 1C MXL merge conflicts."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

try:
    from tools.mxl_merge.mxl_preview import (
        PreviewBundle,
        align_semantic_values,
        build_semantic_preview_bundle,
        build_preview_bundle,
        configured_batch_preview_command,
        configured_preview_command,
        render_document_html_batch,
        render_document_html,
    )
except ModuleNotFoundError:
    from mxl_preview import (  # type: ignore[no-redef]
        PreviewBundle,
        align_semantic_values,
        build_semantic_preview_bundle,
        build_preview_bundle,
        configured_batch_preview_command,
        configured_preview_command,
        render_document_html_batch,
        render_document_html,
    )

try:
    from tools.mxl_merge.mxl_html import safe_json_for_script
except ModuleNotFoundError:
    from mxl_html import safe_json_for_script  # type: ignore[no-redef]

try:
    from tools.mxl_merge.mxl_tool import (
        MergeResult,
        MxlDocument,
        MxlFormatError,
        atomic_write_bytes,
        driver_report_path,
        load_document,
        merge_documents,
        parse_document,
        resolve_documents,
        semantic_coordinates,
        semantic_entries,
        semantic_values,
    )
except ModuleNotFoundError:
    from mxl_tool import (  # type: ignore[no-redef]
        MergeResult,
        MxlDocument,
        MxlFormatError,
        atomic_write_bytes,
        driver_report_path,
        load_document,
        merge_documents,
        parse_document,
        resolve_documents,
        semantic_coordinates,
        semantic_entries,
        semantic_values,
    )

try:
    from tools.mxl_merge.mxl_onec import (
        MxlEditorError,
        launch_mxl_editor,
        mxl_editor_available,
    )
except ModuleNotFoundError:
    from mxl_onec import (  # type: ignore[no-redef]
        MxlEditorError,
        launch_mxl_editor,
        mxl_editor_available,
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
    preview_loading: bool = False
    preview_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    file_editor: str | None = None
    file_editor_enabled: bool | None = None
    editor_workspace: Path | None = None
    edited_result_path: Path | None = None
    edited_result_data: bytes | None = None
    generated_result_data: bytes | None = None
    edited_resolutions_key: str | None = None
    manual_changes: list[dict[str, object]] = field(default_factory=list)
    manual_unmapped: bool = False
    editor_running: bool = False
    editor_error: str | None = None
    editor_revision: int = 0
    editor_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

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

    def configure_file_editor(self, file_editor: str | None = None) -> None:
        self.file_editor = file_editor
        self.file_editor_enabled = mxl_editor_available(file_editor)

    def file_editor_model(self) -> dict[str, object]:
        return {
            "available": (
                self.file_editor_enabled
                if self.file_editor_enabled is not None
                else mxl_editor_available(self.file_editor)
            ),
            "active": self.edited_result_path is not None,
        }

    def _ensure_editor_workspace(self) -> Path:
        if self.editor_workspace is None:
            self.editor_workspace = Path(
                tempfile.mkdtemp(prefix="mxl-merge-editor-")
            )
        return self.editor_workspace

    def open_source_copy(self, side: str) -> tuple[Path, subprocess.Popen[bytes] | None]:
        documents = {
            "base": self.base,
            "local": self.local,
            "remote": self.remote,
        }
        if side not in documents:
            raise ValueError(f"Unknown MXL source: {side}")
        workspace = self._ensure_editor_workspace()
        snapshot = workspace / f"{side}-read-only.mxl"
        atomic_write_bytes(snapshot, documents[side].data)
        try:
            snapshot.chmod(0o444)
        except OSError:
            pass
        return snapshot, launch_mxl_editor(snapshot, self.file_editor)

    @staticmethod
    def _resolutions_key(
        resolutions: Mapping[str, Mapping[str, object]]
    ) -> str:
        return json.dumps(
            resolutions,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _manual_change_model(
        self, generated: MxlDocument, edited: MxlDocument
    ) -> tuple[list[dict[str, object]], bool]:
        generated_values = semantic_values(generated)
        edited_values = semantic_values(edited)
        generated_coordinates = semantic_coordinates(generated)
        edited_coordinates = semantic_coordinates(edited)
        alignment = align_semantic_values(
            generated_values, generated_values, edited_values
        )
        changes: list[dict[str, object]] = []
        for row in alignment.rows:
            before = row.get("base")
            after = row.get("remote")
            if before == after:
                continue
            indices = row.get("indices", {})
            generated_index = (
                indices.get("base") if isinstance(indices, Mapping) else None
            )
            edited_index = (
                indices.get("remote") if isinstance(indices, Mapping) else None
            )
            operation = (
                "add" if before is None else "delete" if after is None else "edit"
            )
            changes.append(
                {
                    "operation": operation,
                    "before": before,
                    "after": after,
                    "generated_index": generated_index,
                    "edited_index": edited_index,
                    "generated_coordinate": (
                        generated_coordinates[generated_index]
                        if isinstance(generated_index, int)
                        and generated_index < len(generated_coordinates)
                        else None
                    ),
                    "edited_coordinate": (
                        edited_coordinates[edited_index]
                        if isinstance(edited_index, int)
                        and edited_index < len(edited_coordinates)
                        else None
                    ),
                }
            )
        # Byte-level differences without semantic changes usually mean manual
        # formatting or other MXL metadata edits that cannot be tied to a cell.
        return changes, generated.data != edited.data and not changes

    def begin_manual_edit(
        self,
        resolutions: Mapping[str, Mapping[str, object]],
        on_exit: Callable[[], None] | None = None,
    ) -> tuple[MergeResult, Path | None]:
        result = resolve_documents(self.base, self.local, self.remote, resolutions)
        if not result.success or result.data is None:
            return result, None
        document = parse_document(result.data, "<editable-result>")
        workspace = self._ensure_editor_workspace()
        editable = workspace / "merged-editable.mxl"
        key = self._resolutions_key(resolutions)
        reopening = False
        with self.editor_lock:
            if self.editor_running:
                return (
                    MergeResult(False, None, "The MXL editor is already running"),
                    None,
                )
            if (
                self.edited_result_path is not None
                and self.edited_resolutions_key != key
            ):
                return (
                    MergeResult(
                        False,
                        None,
                        "Discard manual edits before changing merge decisions",
                    ),
                    None,
                )
            reopening = self.edited_result_path is not None
            if not reopening:
                atomic_write_bytes(editable, result.data)
                self.edited_result_data = result.data
                # Keep this immutable baseline across every later editor
                # session so all manual edits remain attributable to the
                # original merged result.
                self.generated_result_data = result.data
                self.edited_resolutions_key = key
                self.manual_changes = []
                self.manual_unmapped = False
                self.editor_revision += 1
            self.edited_result_path = editable
            self.editor_error = None
        del document
        try:
            process = launch_mxl_editor(editable, self.file_editor)
        except MxlEditorError as error:
            with self.editor_lock:
                self.editor_running = False
                self.editor_error = str(error)
                if not reopening:
                    self.edited_result_path = None
                    self.edited_result_data = None
                    self.generated_result_data = None
                    self.edited_resolutions_key = None
            raise
        with self.editor_lock:
            self.editor_running = process is not None
        if process is not None:
            def wait_for_editor() -> None:
                process.wait()
                with self.editor_lock:
                    self.editor_running = False
                if on_exit is not None:
                    on_exit()

            threading.Thread(
                target=wait_for_editor,
                name="mxl-file-editor",
                daemon=True,
            ).start()
        return result, editable

    def reload_manual_edit(self) -> dict[str, object]:
        with self.editor_lock:
            path = self.edited_result_path
            generated_data = self.generated_result_data
        if path is None or generated_data is None:
            raise ValueError("The merged result is not open for manual editing")
        data = path.read_bytes()
        edited = parse_document(data, str(path))
        generated = parse_document(generated_data, "<generated-result>")
        changes, unmapped = self._manual_change_model(generated, edited)
        with self.editor_lock:
            self.edited_result_data = data
            self.manual_changes = changes
            self.manual_unmapped = unmapped
            self.editor_error = None
            self.editor_revision += 1
        return self.editor_status()

    def discard_manual_edits(self) -> None:
        with self.editor_lock:
            self.edited_result_path = None
            self.edited_result_data = None
            self.generated_result_data = None
            self.edited_resolutions_key = None
            self.manual_changes = []
            self.manual_unmapped = False
            self.editor_running = False
            self.editor_error = None
            self.editor_revision += 1

    def close_unchanged_manual_edit(self) -> bool:
        with self.editor_lock:
            if (
                self.edited_result_path is None
                or self.editor_running
                or self.manual_changes
                or self.manual_unmapped
            ):
                return False
        self.discard_manual_edits()
        return True

    def editor_status(self) -> dict[str, object]:
        with self.editor_lock:
            counts = {
                operation: sum(
                    change.get("operation") == operation
                    for change in self.manual_changes
                )
                for operation in ("edit", "add", "delete")
            }
            return {
                "available": (
                    self.file_editor_enabled
                    if self.file_editor_enabled is not None
                    else mxl_editor_available(self.file_editor)
                ),
                "active": self.edited_result_path is not None,
                "running": self.editor_running,
                "revision": self.editor_revision,
                "changes": list(self.manual_changes),
                "counts": counts,
                "unmapped": self.manual_unmapped,
                "changed": bool(self.manual_changes or self.manual_unmapped),
                "error": self.editor_error,
            }

    def cleanup(self) -> None:
        workspace = self.editor_workspace
        self.editor_workspace = None
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)

    def prepare_previews(
        self,
        preview_command: str | None = None,
        batch_preview_command: str | None = None,
        *,
        defer_external: bool = False,
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
        documents = {"base": self.base, "local": self.local, "remote": self.remote}
        if defer_external and (self.preview_command or self.batch_preview_command):
            self.preview_bundle = build_semantic_preview_bundle(documents)
            self.preview_loading = True
        else:
            self.preview_bundle = build_preview_bundle(
                documents,
                self.preview_command,
                self.batch_preview_command,
            )
            self.preview_loading = False

    def start_deferred_previews(self) -> None:
        if not self.preview_loading:
            return

        def render() -> None:
            documents = {"base": self.base, "local": self.local, "remote": self.remote}
            try:
                bundle = build_preview_bundle(
                    documents,
                    self.preview_command,
                    self.batch_preview_command,
                )
            except Exception as error:  # Keep the semantic UI usable for provider failures.
                semantic = build_semantic_preview_bundle(documents).semantic
                bundle = PreviewBundle(semantic, {}, None, {"provider": str(error)})
            with self.preview_lock:
                self.preview_bundle = bundle
                self.preview_loading = False

        threading.Thread(target=render, name="mxl-preview-render", daemon=True).start()

    def rendered_preview_model(self) -> dict[str, object]:
        with self.preview_lock:
            preview = self.preview_bundle
            if preview is None:
                return {"provider": None, "available": [], "errors": {}, "loading": False}
            return {
                "provider": preview.renderer,
                "available": list(preview.rendered_html),
                "errors": preview.errors,
                "loading": self.preview_loading,
            }

    def model(self) -> dict[str, Any]:
        result = self.initial_result()
        if self.preview_bundle is None:
            self.prepare_previews()
        assert self.preview_bundle is not None
        preview = self.preview_bundle

        semantic_rows = [dict(row) for row in preview.semantic.rows]
        base_entries = semantic_entries(self.base)
        coordinates_by_side = {
            "base": semantic_coordinates(self.base),
            "local": semantic_coordinates(self.local),
            "remote": semantic_coordinates(self.remote),
        }
        conflict_keys = {
            str(conflict["token_index"])
            for conflict in result.conflicts
            if conflict["kind"] == "value" and conflict.get("token_index") is not None
        }
        # A cell Base never had has no token to hang a field on, so its conflict
        # is matched to the preview row by the coordinate it occupies instead.
        cell_conflict_by_coordinate: dict[str, str] = {}
        for conflict in result.conflicts:
            cell = conflict.get("cell") if conflict["kind"] == "value" else None
            if isinstance(cell, Mapping):
                coordinate = f"R{int(cell['row']) + 1}C{int(cell['column']) + 1}"
                cell_conflict_by_coordinate[coordinate] = str(conflict["key"])
        row_conflict_by_anchor: dict[str, dict[int, str]] = {
            "base": {},
            "local": {},
            "remote": {},
        }
        for conflict in result.conflicts:
            if conflict["kind"] != "row":
                continue
            anchors = conflict.get("field_anchors", {})
            if not isinstance(anchors, Mapping):
                continue
            for side in ("base", "local", "remote"):
                side_anchors = anchors.get(side, [])
                if isinstance(side_anchors, list):
                    for anchor in side_anchors:
                        if isinstance(anchor, int):
                            row_conflict_by_anchor[side][anchor] = str(conflict["key"])

        fields_by_conflict: dict[str, list[str]] = {}
        automatic_value_decisions: dict[str, dict[str, Any]] = {}
        for row_index, row in enumerate(semantic_rows):
            field_id = f"field-{row_index}"
            row["id"] = field_id
            indices = row.get("indices", {})
            row["coordinates"] = {
                side: (
                    coordinates_by_side[side][side_index]
                    if isinstance(indices, Mapping)
                    and isinstance((side_index := indices.get(side)), int)
                    and side_index < len(coordinates_by_side[side])
                    else None
                )
                for side in ("base", "local", "remote")
            }
            row_conflict_keys = {
                row_conflict_by_anchor[side][side_index]
                for side in ("base", "local", "remote")
                if isinstance(indices, Mapping)
                and isinstance((side_index := indices.get(side)), int)
                and side_index in row_conflict_by_anchor[side]
            }
            if len(row_conflict_keys) == 1:
                row_conflict_key = row_conflict_keys.pop()
                row["row_conflict_key"] = row_conflict_key
                row["conflict_key"] = row_conflict_key
                fields_by_conflict.setdefault(row_conflict_key, []).append(field_id)
            if row.get("base") is None and cell_conflict_by_coordinate:
                coordinates = row["coordinates"]
                cell_key = next(
                    (
                        cell_conflict_by_coordinate[coordinate]
                        for side in ("local", "remote")
                        if (coordinate := coordinates.get(side))
                        in cell_conflict_by_coordinate
                    ),
                    None,
                )
                if cell_key is not None:
                    row["conflict_key"] = cell_key
                    fields_by_conflict.setdefault(cell_key, []).append(field_id)
            anchor = row.get("anchor")
            if (
                row.get("base") is not None
                and isinstance(anchor, int)
                and anchor < len(base_entries)
            ):
                conflict_key = str(base_entries[anchor][0])
                if conflict_key in conflict_keys:
                    row["conflict_key"] = conflict_key
                    fields_by_conflict.setdefault(conflict_key, []).append(field_id)
                else:
                    base_value = row.get("base")
                    local_value = row.get("local")
                    remote_value = row.get("remote")
                    default_choice: str | None = None
                    if (
                        local_value is not None
                        and remote_value is not None
                        and local_value == remote_value
                        and local_value != base_value
                    ):
                        default_choice = "local"
                    elif local_value == base_value and remote_value not in {None, base_value}:
                        default_choice = "remote"
                    elif remote_value == base_value and local_value not in {None, base_value}:
                        default_choice = "local"
                    if default_choice is not None:
                        token_index = base_entries[anchor][0]
                        row["conflict_key"] = conflict_key
                        fields_by_conflict.setdefault(conflict_key, []).append(field_id)
                        automatic_value_decisions[conflict_key] = {
                            "kind": "value",
                            "key": conflict_key,
                            "token_index": token_index,
                            "token_type": self.base.tokens[token_index].kind,
                            "base": base_value,
                            "local": local_value,
                            "remote": remote_value,
                            "default_choice": default_choice,
                            "requires_choice": False,
                            "automatic": True,
                        }

        rows_by_id = {str(row["id"]): row for row in semantic_rows}

        def attach_field_metadata(item: dict[str, Any]) -> None:
            field_ids = fields_by_conflict.get(item["key"], [])
            if not field_ids:
                return
            item["field_id"] = field_ids[0]
            item["field_ids"] = field_ids
            linked_rows = [rows_by_id[field_id] for field_id in field_ids]
            item["coordinates"] = {
                side: list(
                    dict.fromkeys(
                        coordinate
                        for row in linked_rows
                        if (coordinate := row.get("coordinates", {}).get(side))
                    )
                )
                for side in ("base", "local", "remote")
            }

        conflicts: list[dict[str, Any]] = []
        for conflict in result.conflicts:
            item = dict(conflict)
            if conflict["kind"] == "structural":
                item["key"] = "structural"
            elif conflict["kind"] == "row":
                item["key"] = str(conflict["key"])
            else:
                # A cell Base never had carries no token to key on and brings its
                # own coordinate key instead.
                item["key"] = str(conflict.get("key") or conflict["token_index"])
            item["manual_allowed"] = conflict.get("token_type") in {"string", "atom"}
            attach_field_metadata(item)
            conflicts.append(item)
        for item in automatic_value_decisions.values():
            item["manual_allowed"] = item.get("token_type") in {"string", "atom"}
            attach_field_metadata(item)
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
            "fileEditor": self.file_editor_model(),
            "conflicts": conflicts,
            "previews": {
                "semantic": {
                    "rows": semantic_rows,
                    "total": preview.semantic.total_rows,
                    "truncated": preview.semantic.truncated,
                    "stats": preview.semantic.stats,
                },
                "rendered": self.rendered_preview_model(),
            },
        }

    def render_result_preview(
        self, resolutions: Mapping[str, Mapping[str, object]]
    ) -> tuple[MergeResult, bytes | None]:
        result = resolve_documents(self.base, self.local, self.remote, resolutions)
        if not result.success or result.data is None:
            return result, None
        with self.editor_lock:
            if (
                self.edited_result_path is not None
                and self.edited_resolutions_key == self._resolutions_key(resolutions)
                and self.edited_result_data is not None
            ):
                result = MergeResult(True, self.edited_result_data, "Manual MXL edits loaded")
        if not self.preview_command and not self.batch_preview_command:
            return result, None
        document = parse_document(result.data, "<merged-preview>")
        if self.batch_preview_command:
            return result, render_document_html_batch(
                document, self.batch_preview_command
            )
        assert self.preview_command is not None
        return result, render_document_html(document, self.preview_command)

    def render_edited_result(self) -> bytes | None:
        with self.editor_lock:
            data = self.edited_result_data
        if data is None or (not self.preview_command and not self.batch_preview_command):
            return None
        document = parse_document(data, "<manually-edited-result>")
        if self.batch_preview_command:
            return render_document_html_batch(document, self.batch_preview_command)
        assert self.preview_command is not None
        return render_document_html(document, self.preview_command)

    def resolve(self, resolutions: Mapping[str, Mapping[str, object]]) -> MergeResult:
        result = resolve_documents(
            self.base, self.local, self.remote, resolutions
        )
        if not result.success or result.data is None:
            return result
        with self.editor_lock:
            if self.edited_result_path is not None:
                if self.edited_resolutions_key != self._resolutions_key(resolutions):
                    return MergeResult(
                        False,
                        None,
                        "Merge decisions changed after manual editing started",
                    )
                assert self.edited_result_data is not None
                result = MergeResult(
                    True, self.edited_result_data, "Saved with manual MXL edits"
                )

        # Parse before and after writing so the UI never reports success for a
        # malformed serialization and Git never receives a corrupt output.
        parse_document(result.data, str(self.output_path))
        atomic_write_bytes(self.output_path, result.data)
        try:
            driver_report_path(self.output_path).unlink()
        except FileNotFoundError:
            pass
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
        self.result_lock = threading.Lock()

    def refresh_edited_result(self) -> dict[str, object]:
        try:
            status = self.session.reload_manual_edit()
            html = self.session.render_edited_result()
        except (OSError, MxlFormatError, RuntimeError, ValueError) as error:
            with self.session.editor_lock:
                self.session.editor_error = str(error)
                self.session.editor_revision += 1
            return self.session.editor_status()
        if html is not None:
            with self.result_lock:
                self.result_html = html
                self.result_revision += 1
        status["previewRevision"] = self.result_revision
        return status

    def finish_edited_result(self) -> dict[str, object]:
        status = self.refresh_edited_result()
        if not status.get("error") and self.session.close_unchanged_manual_edit():
            status = self.session.editor_status()
            status["previewRevision"] = self.result_revision
            status["closedUnchanged"] = True
        return status

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            session = getattr(self, "session", None)
            if session is not None:
                session.cleanup()


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
        if path == f"/api/{self.server.token}/edit-status":
            status = self.server.session.editor_status()
            status["previewRevision"] = self.server.result_revision
            self._send_json(HTTPStatus.OK, status)
            return
        if path == f"/api/{self.server.token}/preview-status":
            self._send_json(HTTPStatus.OK, self.server.session.rendered_preview_model())
            return
        preview_prefix = f"/preview/{self.server.token}/"
        if path.startswith(preview_prefix):
            side = path.removeprefix(preview_prefix)
            if side == "result":
                with self.server.result_lock:
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
        is_resolve = self._authorized_api_path("resolve")
        is_open_source = self._authorized_api_path("open-source")
        is_edit_result = self._authorized_api_path("edit-result")
        is_reload_edited = self._authorized_api_path("reload-edited")
        is_discard_edited = self._authorized_api_path("discard-edited")
        if not any(
            (
                is_result_preview,
                is_resolve,
                is_open_source,
                is_edit_result,
                is_reload_edited,
                is_discard_edited,
            )
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        payload = self._read_payload()
        if payload is None:
            return

        if is_open_source:
            side = payload.get("side")
            if not isinstance(side, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid source"})
                return
            try:
                snapshot, _ = self.server.session.open_source_copy(side)
            except (OSError, RuntimeError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(
                HTTPStatus.OK,
                {"status": "opened", "side": side, "snapshot": str(snapshot)},
            )
            return

        if is_reload_edited:
            status = self.server.refresh_edited_result()
            if status.get("error"):
                self._send_json(HTTPStatus.BAD_REQUEST, status)
            else:
                self._send_json(HTTPStatus.OK, status)
            return

        if is_discard_edited:
            self.server.session.discard_manual_edits()
            with self.server.result_lock:
                self.server.result_html = None
                self.server.result_revision += 1
            status = self.server.session.editor_status()
            status["previewRevision"] = self.server.result_revision
            self._send_json(HTTPStatus.OK, status)
            return

        resolutions = payload.get("resolutions", {})
        if not isinstance(resolutions, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid resolutions"})
            return

        if is_edit_result:
            try:
                result, path = self.server.session.begin_manual_edit(
                    resolutions, self.server.finish_edited_result
                )
            except (OSError, RuntimeError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if not result.success:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": result.reason, "conflicts": result.conflicts},
                )
                return
            status = self.server.session.editor_status()
            status.update(
                {
                    "status": "opened",
                    "path": str(path),
                    "previewRevision": self.server.result_revision,
                }
            )
            self._send_json(HTTPStatus.OK, status)
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
            with self.server.result_lock:
                self.server.result_html = html
                self.server.result_revision += 1
                revision = self.server.result_revision
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "rendered",
                    "reason": result.reason,
                    "revision": revision,
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


def render_ui(model: Mapping[str, object], token: str) -> bytes:
    template_path = Path(__file__).with_name("ui.html")
    template = template_path.read_text(encoding="utf-8")
    html = template.replace("__MXL_MODEL__", safe_json_for_script(model))
    html = html.replace("__MXL_TOKEN__", safe_json_for_script(token))
    return html.encode("utf-8")


def create_ui_server(
    session: UiSession,
    host: str = "127.0.0.1",
    port: int = 0,
    preview_command: str | None = None,
    batch_preview_command: str | None = None,
    file_editor: str | None = None,
) -> tuple[MxlUiServer, str]:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The MXL merge UI can only bind to a loopback address")
    session.prepare_previews(
        preview_command, batch_preview_command, defer_external=True
    )
    session.configure_file_editor(file_editor)
    token = secrets.token_urlsafe(24)
    server = MxlUiServer(
        (host, port), session, token, render_ui(session.model(), token)
    )
    actual_host, actual_port = server.server_address[:2]
    url_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    session.start_deferred_previews()
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
    file_editor: str | None = None,
) -> int:
    try:
        session = UiSession.from_paths(base_path, local_path, remote_path, output_path)
        server, url = create_ui_server(
            session,
            host,
            port,
            preview_command,
            batch_preview_command,
            file_editor,
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
