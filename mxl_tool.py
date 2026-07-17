#!/usr/bin/env python3
"""Safe semantic diff and three-way merge support for 1C MOXCEL files."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


MAGIC = b"MOXCEL"
UTF8_BOM = b"\xef\xbb\xbf"
VOLATILE_REF_RE = re.compile(r"^(?P<index>\d+):(?P<uuid>[0-9a-fA-F]{32})$")
MXL_ATTRIBUTES_LINE = "*.mxl -text diff=mxl merge=mxl"


class MxlFormatError(ValueError):
    """Raised when a file is not a supported MOXCEL serialization."""


@dataclass(frozen=True)
class Token:
    kind: str
    raw: str
    value: str
    start: int
    end: int

    @property
    def semantic_value(self) -> tuple[str, str]:
        value = self.value
        if self.kind == "atom":
            match = VOLATILE_REF_RE.fullmatch(value)
            if match:
                value = f"*:{match.group('uuid').lower()}"
        return self.kind, value

    @property
    def structural_value(self) -> tuple[str, str | None]:
        if self.kind == "punctuation":
            return self.kind, self.value
        return self.kind, None


@dataclass(frozen=True)
class MxlDocument:
    path: str
    data: bytes
    prefix: bytes
    text: str
    tokens: tuple[Token, ...]

    @property
    def semantic_sequence(self) -> tuple[tuple[str, str], ...]:
        return tuple(token.semantic_value for token in self.tokens)

    @property
    def structural_sequence(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(token.structural_value for token in self.tokens)


@dataclass(frozen=True)
class MergeResult:
    success: bool
    data: bytes | None
    reason: str
    conflicts: tuple[dict[str, object], ...] = ()


Resolution = Mapping[str, object]


def _decode_quoted_string(text: str, start: int) -> tuple[str, int]:
    index = start + 1
    value: list[str] = []
    while index < len(text):
        character = text[index]
        if character != '"':
            value.append(character)
            index += 1
            continue

        if index + 1 < len(text) and text[index + 1] == '"':
            value.append('"')
            index += 2
            continue

        return "".join(value), index + 1

    raise MxlFormatError(f"Unterminated quoted string at character {start}")


def tokenize(text: str) -> tuple[Token, ...]:
    tokens: list[Token] = []
    index = 0
    depth = 0

    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue

        if character in "{},":
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth < 0:
                    raise MxlFormatError(f"Unexpected closing brace at character {index}")
            tokens.append(Token("punctuation", character, character, index, index + 1))
            index += 1
            continue

        if character == '"':
            value, end = _decode_quoted_string(text, index)
            tokens.append(Token("string", text[index:end], value, index, end))
            index = end
            continue

        end = index
        while end < len(text) and not text[end].isspace() and text[end] not in "{},\"":
            end += 1
        if end == index:
            raise MxlFormatError(f"Unexpected character {text[index]!r} at character {index}")
        raw = text[index:end]
        tokens.append(Token("atom", raw, raw, index, end))
        index = end

    if depth != 0:
        raise MxlFormatError(f"Unbalanced braces: final nesting depth is {depth}")
    if not tokens or tokens[0].value != "{" or tokens[-1].value != "}":
        raise MxlFormatError("MOXCEL payload must contain one root structure")

    return tuple(tokens)


def load_document(path: str | Path) -> MxlDocument:
    file_path = Path(path)
    return parse_document(file_path.read_bytes(), str(file_path))


def parse_document(data: bytes, path: str = "<memory>") -> MxlDocument:
    if not data.startswith(MAGIC):
        raise MxlFormatError(f"{path}: missing MOXCEL signature")

    bom_position = data.find(UTF8_BOM, len(MAGIC), 128)
    if bom_position < 0:
        raise MxlFormatError(f"{path}: UTF-8 payload marker was not found")

    body_start = bom_position + len(UTF8_BOM)
    prefix = data[:body_start]
    try:
        text = data[body_start:].decode("utf-8")
    except UnicodeDecodeError as error:
        raise MxlFormatError(f"{path}: payload is not valid UTF-8: {error}") from error

    return MxlDocument(path, data, prefix, text, tokenize(text))


def _replace_tokens(document: MxlDocument, replacements: dict[int, str]) -> bytes:
    text = document.text
    for token_index in sorted(replacements, reverse=True):
        token = document.tokens[token_index]
        text = text[: token.start] + replacements[token_index] + text[token.end :]
    return document.prefix + text.encode("utf-8")


def _display_token(token: Token) -> str:
    if token.kind == "string":
        return token.value
    return token.raw


def _nearby_strings(tokens: Sequence[Token], token_index: int, limit: int = 4) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for index, token in enumerate(tokens):
        if token.kind != "string" or token.value in {"", "#"}:
            continue
        candidates.append((abs(index - token_index), token.value))
    candidates.sort(key=lambda item: item[0])
    return [value for _, value in candidates[:limit]]


def merge_documents(base: MxlDocument, local: MxlDocument, remote: MxlDocument) -> MergeResult:
    if local.data == remote.data:
        return MergeResult(True, local.data, "Both sides are byte-for-byte identical")
    if local.data == base.data:
        return MergeResult(True, remote.data, "Only the remote side changed")
    if remote.data == base.data:
        return MergeResult(True, local.data, "Only the local side changed")
    if local.semantic_sequence == remote.semantic_sequence:
        return MergeResult(True, local.data, "Both sides are semantically identical")

    structures_match = (
        base.structural_sequence == local.structural_sequence == remote.structural_sequence
    )
    if not structures_match:
        if local.semantic_sequence == base.semantic_sequence:
            return MergeResult(True, remote.data, "Only the remote side changed semantically")
        if remote.semantic_sequence == base.semantic_sequence:
            return MergeResult(True, local.data, "Only the local side changed semantically")
        return MergeResult(
            False,
            None,
            "Both sides changed the serialized MXL structure",
            (
                {
                    "kind": "structural",
                    "base_token_count": len(base.tokens),
                    "local_token_count": len(local.tokens),
                    "remote_token_count": len(remote.tokens),
                },
            ),
        )

    replacements: dict[int, str] = {}
    conflicts: list[dict[str, object]] = []

    for index, (base_token, local_token, remote_token) in enumerate(
        zip(base.tokens, local.tokens, remote.tokens, strict=True)
    ):
        base_value = base_token.semantic_value
        local_value = local_token.semantic_value
        remote_value = remote_token.semantic_value

        if local_value == remote_value:
            continue
        if local_value == base_value:
            replacements[index] = remote_token.raw
            continue
        if remote_value == base_value:
            continue

        conflicts.append(
            {
                "kind": "value",
                "token_index": index,
                "token_type": base_token.kind,
                "base": _display_token(base_token),
                "local": _display_token(local_token),
                "remote": _display_token(remote_token),
                "context": _nearby_strings(base.tokens, index),
            }
        )

    if conflicts:
        return MergeResult(False, None, "Both sides changed the same MXL values", tuple(conflicts))

    return MergeResult(
        True,
        _replace_tokens(local, replacements),
        f"Merged {len(replacements)} non-overlapping token change(s)",
    )


def _manual_token_value(token: Token, value: object) -> str:
    text = str(value)
    if token.kind == "string":
        return f'"{text.replace(chr(34), chr(34) * 2)}"'
    if token.kind == "atom":
        if not text or any(character.isspace() or character in '{},"' for character in text):
            raise MxlFormatError("Manual atom values cannot contain whitespace or MXL punctuation")
        return text
    raise MxlFormatError(f"Manual resolution is not supported for {token.kind} tokens")


def _resolved_token_raw(
    resolution: Resolution,
    base_token: Token,
    local_token: Token,
    remote_token: Token,
) -> str:
    choice = resolution.get("choice")
    if choice == "base":
        return base_token.raw
    if choice == "local":
        return local_token.raw
    if choice == "remote":
        return remote_token.raw
    if choice == "manual":
        return _manual_token_value(base_token, resolution.get("value", ""))
    raise MxlFormatError(f"Unknown MXL conflict resolution choice: {choice!r}")


def resolve_documents(
    base: MxlDocument,
    local: MxlDocument,
    remote: MxlDocument,
    resolutions: Mapping[str, Resolution],
) -> MergeResult:
    """Resolve a merge using choices produced by the visual conflict resolver.

    Value-conflict keys are token indexes converted to strings. A two-sided
    structural conflict uses the special ``structural`` key and accepts a
    whole-file base/local/remote choice.
    """

    automatic_result = merge_documents(base, local, remote)
    if automatic_result.success:
        return automatic_result

    structural_conflict = next(
        (conflict for conflict in automatic_result.conflicts if conflict["kind"] == "structural"),
        None,
    )
    if structural_conflict is not None:
        resolution = resolutions.get("structural")
        if resolution is None:
            return MergeResult(False, None, "The structural conflict has not been resolved")
        choice = resolution.get("choice")
        documents = {"base": base, "local": local, "remote": remote}
        if choice not in documents:
            return MergeResult(False, None, f"Unsupported structural conflict choice: {choice!r}")
        selected = documents[str(choice)]
        return MergeResult(True, selected.data, f"Selected the complete {choice} MXL document")

    if not (
        base.structural_sequence == local.structural_sequence == remote.structural_sequence
    ):
        return MergeResult(False, None, "MXL structures do not match")

    replacements: dict[int, str] = {}
    unresolved: list[dict[str, object]] = []
    resolved_count = 0

    for index, (base_token, local_token, remote_token) in enumerate(
        zip(base.tokens, local.tokens, remote.tokens, strict=True)
    ):
        base_value = base_token.semantic_value
        local_value = local_token.semantic_value
        remote_value = remote_token.semantic_value

        if local_value == remote_value:
            continue
        if local_value == base_value:
            replacements[index] = remote_token.raw
            continue
        if remote_value == base_value:
            continue

        resolution = resolutions.get(str(index))
        if resolution is None:
            unresolved.append(
                {
                    "kind": "value",
                    "token_index": index,
                    "base": _display_token(base_token),
                    "local": _display_token(local_token),
                    "remote": _display_token(remote_token),
                }
            )
            continue

        replacement = _resolved_token_raw(
            resolution, base_token, local_token, remote_token
        )
        if replacement != local_token.raw:
            replacements[index] = replacement
        resolved_count += 1

    if unresolved:
        return MergeResult(
            False,
            None,
            f"{len(unresolved)} MXL conflict(s) have not been resolved",
            tuple(unresolved),
        )

    return MergeResult(
        True,
        _replace_tokens(local, replacements),
        f"Applied {resolved_count} conflict choice(s) and merged "
        f"{len(replacements)} token change(s)",
    )


def semantic_entries(document: MxlDocument) -> list[tuple[int, str]]:
    """Return visible spreadsheet field token indexes and their values."""
    entries: list[tuple[int, str]] = []
    tokens = document.tokens
    for index in range(len(tokens) - 4):
        window = tokens[index : index + 5]
        if (
            window[0].value == "{"
            and window[1].kind == "string"
            and window[1].value == "#"
            and window[2].value == ","
            and window[3].kind == "string"
            and window[4].value == "}"
        ):
            entries.append((index + 3, window[3].value))
    return entries


def semantic_values(document: MxlDocument) -> list[str]:
    return [value for _, value in semantic_entries(document)]


def textconv(document: MxlDocument) -> str:
    lines = ["# MXL semantic values"]
    for value in semantic_values(document):
        escaped = value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        lines.append(escaped)
    return "\n".join(lines) + "\n"


def _write_report(report_path: Path, target_path: str, result: MergeResult) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "path": target_path,
        "reason": result.reason,
        "conflicts": result.conflicts,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_stale_report(report_path: Path) -> None:
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass


def _run_merge(
    base_path: str,
    local_path: str,
    remote_path: str,
    output_path: str,
    report_path: str,
    target_path: str,
) -> int:
    try:
        result = merge_documents(
            load_document(base_path),
            load_document(local_path),
            load_document(remote_path),
        )
    except (OSError, MxlFormatError) as error:
        result = MergeResult(False, None, f"Unable to parse MXL input: {error}")

    report = Path(report_path)
    if not result.success:
        _write_report(report, target_path, result)
        print(f"MXL merge conflict in {target_path}: {result.reason}", file=sys.stderr)
        print(f"Conflict report: {report}", file=sys.stderr)
        return 1

    assert result.data is not None
    Path(output_path).write_bytes(result.data)
    _remove_stale_report(report)
    print(f"MXL merge: {result.reason}", file=sys.stderr)
    return 0


def install_git_config(
    onec_client: str | None = None,
    onec_infobase: str | None = None,
    onec_epf: str | None = None,
    onec_username: str | None = None,
    global_install: bool = False,
) -> int:
    root: Path | None = None
    if not global_install:
        try:
            root = Path(
                subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()
            )
        except subprocess.CalledProcessError as error:
            details = (error.stderr or "").strip()
            message = (
                "Git does not recognize the current directory as a repository; "
                "falling back to global installation."
            )
            if details:
                message += f" Git reported: {details}"
            print(message, file=sys.stderr)
            global_install = True

    config_scope = "--global" if global_install else "--local"
    script_path = Path(__file__).resolve()
    if root is not None and not global_install:
        try:
            script = script_path.relative_to(root).as_posix()
        except ValueError:
            script = str(script_path)
    else:
        script = str(script_path)

    python_executable = Path(sys.executable).resolve().as_posix()
    if getattr(sys, "frozen", False):
        python_command = f'"{python_executable}"'
    else:
        python_command = f'"{python_executable}" "{script}"'
    textconv_command = f'{python_command} textconv'
    # Git shell-quotes merge placeholders before substitution. Adding another
    # quote layer would turn a path such as sample.mxl into the literal file
    # name 'sample.mxl'.
    merge_command = f'{python_command} merge-driver %O %A %B %P'
    ui_command = (
        f'{python_command} ui "$BASE" "$LOCAL" "$REMOTE" '
        '--output "$MERGED"'
    )
    settings = {
        "diff.mxl.textconv": textconv_command,
        "diff.mxl.cachetextconv": "true",
        "merge.mxl.name": "1C MXL semantic merge driver",
        "merge.mxl.driver": merge_command,
        "merge.mxl.recursive": "binary",
        "mergetool.mxl.cmd": ui_command,
        "mergetool.mxl.trustExitCode": "true",
    }
    for key, value in settings.items():
        subprocess.run(["git", "config", config_scope, key, value], check=True)

    onec_settings = {
        "mxl.onecClient": onec_client,
        "mxl.onecInfobase": onec_infobase,
        "mxl.onecEpf": onec_epf,
        "mxl.onecUsername": onec_username,
    }
    for key, value in onec_settings.items():
        if value:
            subprocess.run(["git", "config", config_scope, key, value], check=True)

    configured_client = onec_client or subprocess.run(
        ["git", "config", config_scope, "--get", "mxl.onecClient"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if configured_client:
        preview_command = f'{python_command} render-onec {{input}} {{output}}'
        subprocess.run(
            ["git", "config", config_scope, "mxl.previewCommand", preview_command],
            check=True,
        )
        configured_epf = onec_epf or subprocess.run(
            ["git", "config", config_scope, "--get", "mxl.onecEpf"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        epf_path = (
            Path(configured_epf).expanduser()
            if configured_epf
            else Path(__file__).resolve().parent / "onec" / "MxlToHtml.epf"
        )
        try:
            from tools.mxl_merge.mxl_onec import epf_supports_batch
        except ModuleNotFoundError:
            from mxl_onec import epf_supports_batch  # type: ignore[no-redef]
        if epf_supports_batch(epf_path):
            batch_command = f'{python_command} render-onec-batch {{manifest}}'
            subprocess.run(
                [
                    "git",
                    "config",
                    config_scope,
                    "mxl.previewBatchCommand",
                    batch_command,
                ],
                check=True,
            )

    if global_install:
        _install_global_attributes()
        print("Installed MXL diff and merge drivers in the global Git configuration.")
    else:
        assert root is not None
        _ensure_attributes_file(root / ".gitattributes")
        print("Installed MXL diff and merge drivers in the local Git configuration.")
    if configured_client:
        print(
            "Configured the bundled 1C MXL-to-HTML preview renderer; "
            "its service infobase will be created automatically on first use."
        )
    return 0


def _ensure_attributes_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if MXL_ATTRIBUTES_LINE in {line.strip() for line in text.splitlines()}:
        return
    separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
    path.write_text(f"{text}{separator}{MXL_ATTRIBUTES_LINE}\n", encoding="utf-8")


def _install_global_attributes() -> Path:
    configured = subprocess.run(
        ["git", "config", "--global", "--get", "core.attributesFile"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if configured:
        attributes_path = Path(os.path.expandvars(configured)).expanduser()
    else:
        attributes_path = Path.home() / ".mxl-merge" / "gitattributes"
        subprocess.run(
            [
                "git",
                "config",
                "--global",
                "core.attributesFile",
                str(attributes_path),
            ],
            check=True,
        )
    _ensure_attributes_file(attributes_path)
    return attributes_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate the supported MXL structure")
    validate_parser.add_argument("file")

    textconv_parser = subparsers.add_parser("textconv", help="Print stable semantic text for git diff")
    textconv_parser.add_argument("file")

    merge_parser = subparsers.add_parser("merge", help="Perform a safe three-way MXL merge")
    merge_parser.add_argument("base")
    merge_parser.add_argument("local")
    merge_parser.add_argument("remote")
    merge_parser.add_argument("--output", required=True)
    merge_parser.add_argument("--report")

    driver_parser = subparsers.add_parser("merge-driver", help=argparse.SUPPRESS)
    driver_parser.add_argument("base")
    driver_parser.add_argument("local")
    driver_parser.add_argument("remote")
    driver_parser.add_argument("path")

    ui_parser = subparsers.add_parser("ui", help="Open the visual MXL conflict resolver")
    ui_parser.add_argument("base")
    ui_parser.add_argument("local")
    ui_parser.add_argument("remote")
    ui_parser.add_argument("--output", required=True)
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=0)
    ui_parser.add_argument(
        "--preview-command",
        help="Trusted command template that converts {input} MXL to {output} HTML",
    )
    ui_parser.add_argument(
        "--preview-batch-command",
        help="Trusted command template that converts a {manifest} in one process",
    )
    ui_parser.add_argument(
        "--no-browser", action="store_true", help="Print the URL without opening a browser"
    )

    render_parser = subparsers.add_parser(
        "render-onec", help="Render an MXL file to HTML using 1C:Enterprise"
    )
    render_parser.add_argument("input")
    render_parser.add_argument("output")
    render_parser.add_argument("--client", help="Path to 1cv8c.exe")
    render_parser.add_argument(
        "--infobase", help="Optional renderer file infobase; created automatically by default"
    )
    render_parser.add_argument("--epf", help="Path to MxlToHtml.epf")
    render_parser.add_argument("--username")
    render_parser.add_argument("--password")
    render_parser.add_argument("--timeout", type=int, default=120)

    batch_render_parser = subparsers.add_parser(
        "render-onec-batch", help="Render a JSON manifest of MXL files in one 1C process"
    )
    batch_render_parser.add_argument("manifest")
    batch_render_parser.add_argument("--client", help="Path to 1cv8c.exe")
    batch_render_parser.add_argument("--infobase")
    batch_render_parser.add_argument("--epf", help="Path to batch-capable MxlToHtml.epf")
    batch_render_parser.add_argument("--username")
    batch_render_parser.add_argument("--password")
    batch_render_parser.add_argument("--timeout", type=int, default=180)

    install_parser = subparsers.add_parser(
        "install", help="Install drivers into this repository's local Git config"
    )
    install_parser.add_argument("--onec-client", help="Path to 1cv8c.exe")
    install_parser.add_argument(
        "--onec-infobase", help="Optional renderer file infobase override"
    )
    install_parser.add_argument("--onec-epf", help="Path to MxlToHtml.epf; bundled by default")
    install_parser.add_argument("--onec-username")
    install_parser.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Install for all repositories; used automatically outside a repository",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "validate":
            document = load_document(args.file)
            print(
                f"OK: {args.file}: {len(document.tokens)} tokens, "
                f"{len(semantic_values(document))} semantic values"
            )
            return 0
        if args.command == "textconv":
            sys.stdout.write(textconv(load_document(args.file)))
            return 0
        if args.command == "merge":
            report = args.report or f"{args.output}.merge-conflict.json"
            return _run_merge(
                args.base, args.local, args.remote, args.output, report, args.output
            )
        if args.command == "merge-driver":
            report = f"{args.path}.merge-conflict.json"
            return _run_merge(
                args.base, args.local, args.remote, args.local, report, args.path
            )
        if args.command == "ui":
            try:
                from tools.mxl_merge.mxl_ui import run_ui
            except ModuleNotFoundError:
                # Direct execution puts tools/mxl_merge rather than the
                # repository root on sys.path.
                from mxl_ui import run_ui

            return run_ui(
                args.base,
                args.local,
                args.remote,
                args.output,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
                preview_command=args.preview_command,
                batch_preview_command=args.preview_batch_command,
            )
        if args.command == "render-onec":
            try:
                from tools.mxl_merge.mxl_onec import (
                    OneCRenderError,
                    render_mxl_with_onec,
                    resolve_onec_settings,
                )
            except ModuleNotFoundError:
                from mxl_onec import (  # type: ignore[no-redef]
                    OneCRenderError,
                    render_mxl_with_onec,
                    resolve_onec_settings,
                )

            try:
                settings = resolve_onec_settings(
                    client_exe=args.client,
                    infobase=args.infobase,
                    epf=args.epf,
                    username=args.username,
                    password=args.password,
                    timeout_seconds=args.timeout,
                )
                render_mxl_with_onec(args.input, args.output, settings)
            except OneCRenderError as error:
                print(f"mxl-tool: {error}", file=sys.stderr)
                return 2
            return 0
        if args.command == "render-onec-batch":
            try:
                from tools.mxl_merge.mxl_onec import (
                    OneCRenderError,
                    render_mxl_batch_with_onec,
                    resolve_onec_settings,
                )
            except ModuleNotFoundError:
                from mxl_onec import (  # type: ignore[no-redef]
                    OneCRenderError,
                    render_mxl_batch_with_onec,
                    resolve_onec_settings,
                )
            try:
                manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
                manifest_items = manifest.get("items") if isinstance(manifest, dict) else None
                if not isinstance(manifest_items, list) or not manifest_items:
                    raise ValueError("Batch manifest must contain a non-empty items array")
                items: dict[str, tuple[str, str]] = {}
                for index, item in enumerate(manifest_items):
                    if not isinstance(item, dict):
                        raise ValueError(f"Batch item {index} is not an object")
                    name = str(item.get("name") or index)
                    if name in items:
                        raise ValueError(f"Duplicate batch item name: {name}")
                    items[name] = (str(item.get("inputPath") or ""), str(item.get("outputPath") or ""))
                settings = resolve_onec_settings(
                    client_exe=args.client,
                    infobase=args.infobase,
                    epf=args.epf,
                    username=args.username,
                    password=args.password,
                    timeout_seconds=args.timeout,
                )
                render_mxl_batch_with_onec(items, settings)
            except (OneCRenderError, ValueError, json.JSONDecodeError) as error:
                print(f"mxl-tool: {error}", file=sys.stderr)
                return 2
            return 0
        if args.command == "install":
            return install_git_config(
                args.onec_client,
                args.onec_infobase,
                args.onec_epf,
                args.onec_username,
                args.global_install,
            )
    except (OSError, MxlFormatError, subprocess.CalledProcessError) as error:
        print(f"mxl-tool: {error}", file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
