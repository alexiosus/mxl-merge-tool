"""Preview providers for the MXL merge UI.

The built-in provider aligns semantic values without requiring 1C. An optional
external command can render each MXL file to standalone HTML using the 1C
platform (or another trusted converter) for a high-fidelity preview.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Mapping, Sequence

try:
    from tools.mxl_merge.mxl_tool import MxlDocument, semantic_values
except ModuleNotFoundError:
    from mxl_tool import MxlDocument, semantic_values  # type: ignore[no-redef]


PREVIEW_ROW_LIMIT = 5_000
PREVIEW_COMMAND_TIMEOUT_SECONDS = 90
BATCH_PREVIEW_COMMAND_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class SemanticAlignment:
    rows: tuple[dict[str, object], ...]
    total_rows: int
    truncated: bool
    stats: Mapping[str, int]


@dataclass(frozen=True)
class PreviewBundle:
    semantic: SemanticAlignment
    rendered_html: Mapping[str, bytes]
    renderer: str | None
    errors: Mapping[str, str]


@dataclass
class _SideAlignment:
    mapped: dict[int, str | None]
    insertions: dict[int, list[str]]


def _align_side(base: Sequence[str], side: Sequence[str]) -> _SideAlignment:
    mapped: dict[int, str | None] = {}
    insertions: dict[int, list[str]] = {}
    matcher = SequenceMatcher(a=base, b=side, autojunk=False)

    for tag, base_start, base_end, side_start, side_end in matcher.get_opcodes():
        base_length = base_end - base_start
        side_length = side_end - side_start
        shared_length = min(base_length, side_length)

        if tag == "equal":
            for offset in range(base_length):
                mapped[base_start + offset] = side[side_start + offset]
            continue

        if tag == "insert":
            insertions.setdefault(base_start, []).extend(side[side_start:side_end])
            continue

        if tag == "delete":
            for base_index in range(base_start, base_end):
                mapped[base_index] = None
            continue

        # A replacement is aligned positionally inside the changed block. Any
        # remaining side values become insertions at the end of the base block.
        for offset in range(shared_length):
            mapped[base_start + offset] = side[side_start + offset]
        for base_index in range(base_start + shared_length, base_end):
            mapped[base_index] = None
        if side_length > shared_length:
            insertions.setdefault(base_start + shared_length, []).extend(
                side[side_start + shared_length : side_end]
            )

    return _SideAlignment(mapped, insertions)


def _row_state(base: str | None, local: str | None, remote: str | None) -> str:
    if base == local == remote:
        return "unchanged"
    if local == remote:
        return "both"
    if local == base:
        return "remote"
    if remote == base:
        return "local"
    if base is None:
        return "insert-conflict" if local is not None and remote is not None else "insert"
    if local is None or remote is None:
        return "delete-conflict" if local != remote else "delete"
    return "conflict"


def align_semantic_values(
    base: Sequence[str], local: Sequence[str], remote: Sequence[str]
) -> SemanticAlignment:
    local_alignment = _align_side(base, local)
    remote_alignment = _align_side(base, remote)
    rows: list[dict[str, object]] = []
    stats: dict[str, int] = {}

    def append_row(
        base_value: str | None,
        local_value: str | None,
        remote_value: str | None,
        anchor: int,
    ) -> None:
        state = _row_state(base_value, local_value, remote_value)
        stats[state] = stats.get(state, 0) + 1
        rows.append(
            {
                "base": base_value,
                "local": local_value,
                "remote": remote_value,
                "state": state,
                "anchor": anchor,
            }
        )

    for base_index in range(len(base) + 1):
        local_insertions = local_alignment.insertions.get(base_index, [])
        remote_insertions = remote_alignment.insertions.get(base_index, [])
        insertion_count = max(len(local_insertions), len(remote_insertions))
        for insertion_index in range(insertion_count):
            local_value = (
                local_insertions[insertion_index]
                if insertion_index < len(local_insertions)
                else None
            )
            remote_value = (
                remote_insertions[insertion_index]
                if insertion_index < len(remote_insertions)
                else None
            )
            append_row(None, local_value, remote_value, base_index)

        if base_index < len(base):
            append_row(
                base[base_index],
                local_alignment.mapped.get(base_index),
                remote_alignment.mapped.get(base_index),
                base_index,
            )

    total_rows = len(rows)
    return SemanticAlignment(
        tuple(rows[:PREVIEW_ROW_LIMIT]),
        total_rows,
        total_rows > PREVIEW_ROW_LIMIT,
        stats,
    )


def configured_preview_command(explicit_command: str | None = None) -> str | None:
    if explicit_command:
        return explicit_command
    environment_command = os.environ.get("MXL_PREVIEW_COMMAND")
    if environment_command:
        return environment_command
    try:
        command = subprocess.check_output(
            ["git", "config", "--get", "mxl.previewCommand"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return command or None


def configured_batch_preview_command(
    explicit_command: str | None = None,
) -> str | None:
    if explicit_command is not None:
        return explicit_command.strip() or None
    environment_command = os.environ.get("MXL_PREVIEW_BATCH_COMMAND")
    if environment_command:
        return environment_command
    try:
        command = subprocess.check_output(
            ["git", "config", "--get", "mxl.previewBatchCommand"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return command or None


def _render_with_command(
    document: MxlDocument, command_template: str, side: str, output_directory: Path
) -> bytes:
    arguments = shlex.split(command_template)
    if not arguments:
        raise ValueError("MXL preview command is empty")
    if not any("{input}" in argument for argument in arguments):
        raise ValueError("MXL preview command must contain {input}")
    if not any("{output}" in argument for argument in arguments):
        raise ValueError("MXL preview command must contain {output}")

    output_path = output_directory / f"{side}.html"
    command = [
        argument.replace("{input}", str(Path(document.path).resolve())).replace(
            "{output}", str(output_path)
        )
        for argument in arguments
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=PREVIEW_COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Preview command exited with {completed.returncode}: {message or 'no details'}"
        )
    if not output_path.is_file():
        raise RuntimeError("Preview command did not create the requested HTML file")
    return output_path.read_bytes()


def render_document_html(
    document: MxlDocument, command_template: str, side: str = "result"
) -> bytes:
    """Render an in-memory MXL document with a configured external renderer."""
    with tempfile.TemporaryDirectory(prefix="mxl-result-preview-") as directory:
        work_directory = Path(directory)
        input_path = work_directory / f"{side}.mxl"
        input_path.write_bytes(document.data)
        materialized = replace(document, path=str(input_path))
        return _render_with_command(
            materialized, command_template, side, work_directory
        )


def _render_with_batch_command(
    documents: Mapping[str, MxlDocument],
    command_template: str,
    output_directory: Path,
) -> dict[str, bytes]:
    arguments = shlex.split(command_template)
    if not arguments or not any("{manifest}" in argument for argument in arguments):
        raise ValueError("MXL batch preview command must contain {manifest}")
    items: list[dict[str, str]] = []
    for side, document in documents.items():
        input_path = output_directory / f"{side}.mxl"
        output_path = output_directory / f"{side}.html"
        input_path.write_bytes(document.data)
        items.append(
            {
                "name": side,
                "inputPath": str(input_path),
                "outputPath": str(output_path),
            }
        )
    manifest_path = output_directory / "batch-manifest.json"
    manifest_path.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    command = [
        argument.replace("{manifest}", str(manifest_path)) for argument in arguments
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=BATCH_PREVIEW_COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Batch preview command exited with {completed.returncode}: "
            f"{message or 'no details'}"
        )
    rendered: dict[str, bytes] = {}
    for item in items:
        output_path = Path(item["outputPath"])
        if not output_path.is_file():
            raise RuntimeError(f"Batch preview did not create {output_path.name}")
        rendered[item["name"]] = output_path.read_bytes()
    return rendered


def render_document_html_batch(
    document: MxlDocument, command_template: str, side: str = "result"
) -> bytes:
    """Render one in-memory document through a batch-capable converter."""
    with tempfile.TemporaryDirectory(prefix="mxl-result-batch-preview-") as directory:
        return _render_with_batch_command(
            {side: document}, command_template, Path(directory)
        )[side]


def build_preview_bundle(
    documents: Mapping[str, MxlDocument],
    preview_command: str | None = None,
    batch_preview_command: str | None = None,
) -> PreviewBundle:
    semantic = align_semantic_values(
        semantic_values(documents["base"]),
        semantic_values(documents["local"]),
        semantic_values(documents["remote"]),
    )
    command = configured_preview_command(preview_command)
    batch_command = configured_batch_preview_command(batch_preview_command)
    if command is None and batch_command is None:
        return PreviewBundle(semantic, {}, None, {})

    rendered: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="mxl-preview-") as directory:
        output_directory = Path(directory)
        if batch_command:
            try:
                rendered.update(
                    _render_with_batch_command(documents, batch_command, output_directory)
                )
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                # A legacy one-document EPF is allowed to fall back without
                # delaying or breaking the existing preview path.
                rendered.clear()
                if command is None:
                    errors["batch"] = str(error)
        if len(rendered) == len(documents):
            return PreviewBundle(semantic, rendered, "external-batch", errors)
        if command is None:
            return PreviewBundle(semantic, {}, None, errors)
        for side, document in documents.items():
            try:
                rendered[side] = _render_with_command(
                    document, command, side, output_directory
                )
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                errors[side] = str(error)

    return PreviewBundle(semantic, rendered, "external", errors)
