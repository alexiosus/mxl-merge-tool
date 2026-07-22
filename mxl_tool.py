#!/usr/bin/env python3
"""Safe semantic diff and three-way merge support for 1C MOXCEL files."""

from __future__ import annotations

import argparse
from bisect import bisect_left
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Mapping, Sequence


MAGIC = b"MOXCEL"
UTF8_BOM = b"\xef\xbb\xbf"
VOLATILE_REF_RE = re.compile(r"^(?P<index>\d+):(?P<uuid>[0-9a-fA-F]{32})$")
MXL_ATTRIBUTES_LINE = "*.mxl -text diff=mxl merge=mxl"
TEXTCONV_FORMAT_VERSION = "2"


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
class StructureNode:
    """A parsed MXL structure with token-index based child locations."""

    start: int
    end: int
    children: tuple["StructureNode | int", ...]


@dataclass(frozen=True)
class MxlDocument:
    path: str
    data: bytes
    prefix: bytes
    text: str
    tokens: tuple[Token, ...]
    root: StructureNode
    scopes: tuple[tuple[int, int], ...]

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


@dataclass(frozen=True)
class RowItem:
    """One serialized spreadsheet row (or a safe row-like test structure)."""

    index: int
    coordinate: int
    start_child: int
    end_child: int
    start_token: int
    end_token: int
    values: tuple[str, ...]

    @property
    def signature(self) -> tuple[str, ...]:
        return self.values


@dataclass(frozen=True)
class RowLayout:
    kind: str
    rows: tuple[RowItem, ...]
    start_child: int
    end_child: int


@dataclass(frozen=True)
class RowAlignment:
    base_to_side: Mapping[int, int]
    inserted: tuple[int, ...]
    moved: frozenset[int]


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


def _tokenize_lexically(text: str) -> tuple[Token, ...]:
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
    return tuple(tokens)


def _parse_structure(tokens: Sequence[Token], start: int = 0) -> tuple[StructureNode, int]:
    if start >= len(tokens) or tokens[start].value != "{":
        raise MxlFormatError(f"Expected opening brace at token {start}")

    children: list[StructureNode | int] = []
    index = start + 1
    if index < len(tokens) and tokens[index].value == "}":
        return StructureNode(start, index + 1, ()), index + 1

    while True:
        if index >= len(tokens):
            raise MxlFormatError(f"Unterminated structure starting at token {start}")
        token = tokens[index]
        if token.value in {",", "}"}:
            raise MxlFormatError(f"Expected a value at token {index}, got {token.value!r}")
        if token.value == "{":
            child, index = _parse_structure(tokens, index)
            children.append(child)
        else:
            children.append(index)
            index += 1

        if index >= len(tokens):
            raise MxlFormatError(f"Unterminated structure starting at token {start}")
        separator = tokens[index].value
        if separator == "}":
            return StructureNode(start, index + 1, tuple(children)), index + 1
        if separator != ",":
            raise MxlFormatError(
                f"Expected comma or closing brace at token {index}, got {separator!r}"
            )
        index += 1


def _build_merge_scopes(
    tokens: Sequence[Token], root: StructureNode
) -> tuple[tuple[int, int], ...]:
    """Choose a conservative identity-bearing structure for every value token.

    A remote-only leaf change is safe to transplant only while the same scope on
    the other side is unchanged. Prefer the nearest ancestor containing a visible
    ``{"#", "value"}`` field; arbitrary strings such as style names are not stable
    identities. Fall back to the complete document when no anchor is available.
    """

    ancestors: list[list[StructureNode]] = [[root] for _ in tokens]

    def visit(node: StructureNode, stack: tuple[StructureNode, ...]) -> None:
        current = stack + (node,)
        ancestors[node.start] = list(current)
        ancestors[node.end - 1] = list(current)
        for child in node.children:
            if isinstance(child, StructureNode):
                visit(child, current)
            else:
                ancestors[child] = list(current)

    visit(root, ())
    anchor_cache: dict[int, bool] = {}
    semantic_anchor_tokens: set[int] = set()
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
            semantic_anchor_tokens.add(index + 3)

    def has_anchor(node: StructureNode) -> bool:
        cache_key = id(node)
        if cache_key in anchor_cache:
            return anchor_cache[cache_key]
        result = False
        for child in node.children:
            if isinstance(child, StructureNode):
                result = result or has_anchor(child)
            else:
                result = result or child in semantic_anchor_tokens
            if result:
                break
        anchor_cache[cache_key] = result
        return result

    scopes: list[tuple[int, int]] = [(root.start, root.end) for _ in tokens]
    for token_index, stack in enumerate(ancestors):
        for node in reversed(stack):
            if has_anchor(node):
                scopes[token_index] = (node.start, node.end)
                break
    return tuple(scopes)


def tokenize(text: str) -> tuple[Token, ...]:
    tokens = _tokenize_lexically(text)
    if not tokens:
        raise MxlFormatError("MOXCEL payload is empty")
    root, end = _parse_structure(tokens)
    if end != len(tokens):
        raise MxlFormatError(f"Unexpected token after the root structure at token {end}")
    return tokens


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

    tokens = _tokenize_lexically(text)
    if not tokens:
        raise MxlFormatError("MOXCEL payload is empty")
    root, end = _parse_structure(tokens)
    if end != len(tokens):
        raise MxlFormatError(f"Unexpected token after the root structure at token {end}")
    return MxlDocument(
        path,
        data,
        prefix,
        text,
        tokens,
        root,
        _build_merge_scopes(tokens, root),
    )


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


def _visible_values_from_tokens(tokens: Sequence[Token]) -> list[str]:
    values: list[str] = []
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
            values.append(window[3].value)
    return values


def _node_visible_values(tokens: Sequence[Token], node: StructureNode) -> tuple[str, ...]:
    return tuple(_visible_values_from_tokens(tokens[node.start : node.end]))


def _integer_child(document: MxlDocument, child: StructureNode | int) -> int | None:
    if not isinstance(child, int):
        return None
    token = document.tokens[child]
    if token.kind != "atom":
        return None
    try:
        return int(token.value)
    except ValueError:
        return None


def _record_row_layout(document: MxlDocument) -> RowLayout | None:
    """Recognize the flat row-record stream used by normal MOXCEL files.

    A record consists of four integer header fields followed by one cell record
    and ``count - 1`` index/cell pairs.  Requiring a run starting at row zero
    avoids interpreting unrelated numeric metadata as spreadsheet rows.
    """

    children = document.root.children
    best: RowLayout | None = None
    for candidate in range(max(0, len(children) - 4)):
        coordinate = _integer_child(document, children[candidate])
        if coordinate != 0:
            continue

        rows: list[RowItem] = []
        position = candidate
        previous_coordinate = -1
        while position + 4 < len(children):
            header = [_integer_child(document, children[position + offset]) for offset in range(4)]
            if any(value is None for value in header):
                break
            row_coordinate, _first_column, cell_count, _flags = header
            assert row_coordinate is not None and cell_count is not None
            if row_coordinate <= previous_coordinate or cell_count < 1:
                break
            record_length = 2 * cell_count + 3
            end_child = position + record_length
            if end_child > len(children):
                break
            cell_children = children[position + 4 : end_child]
            if not isinstance(cell_children[0], StructureNode):
                break
            if any(
                not isinstance(child, int if offset % 2 else StructureNode)
                for offset, child in enumerate(cell_children[1:], start=1)
            ):
                break
            first = children[position]
            last = children[end_child - 1]
            assert isinstance(first, int) and isinstance(last, StructureNode)
            values: list[str] = []
            for child in cell_children:
                if isinstance(child, StructureNode):
                    values.extend(_node_visible_values(document.tokens, child))
            rows.append(
                RowItem(
                    len(rows),
                    row_coordinate,
                    position,
                    end_child,
                    first,
                    last.end,
                    tuple(values),
                )
            )
            previous_coordinate = row_coordinate
            position = end_child

        if len(rows) >= 2 and (best is None or len(rows) > len(best.rows)):
            best = RowLayout("records", tuple(rows), candidate, position)
    return best


def _direct_row_layout(document: MxlDocument) -> RowLayout | None:
    """Recognize a contiguous list of semantic child structures.

    This representation is useful for small/simple MXL documents and can be
    rewritten without touching opaque global metadata.
    """

    candidates: list[tuple[int, StructureNode, tuple[str, ...]]] = []
    for child_index, child in enumerate(document.root.children):
        if not isinstance(child, StructureNode):
            continue
        values = _node_visible_values(document.tokens, child)
        if values:
            candidates.append((child_index, child, values))
    if not candidates:
        return None
    first = candidates[0][0]
    last = candidates[-1][0] + 1
    if [index for index, _, _ in candidates] != list(range(first, last)):
        return None
    rows = tuple(
        RowItem(
            index,
            index,
            child_index,
            child_index + 1,
            child.start,
            child.end,
            values,
        )
        for index, (child_index, child, values) in enumerate(candidates)
    )
    return RowLayout("direct", rows, first, last)


def _row_layout(document: MxlDocument) -> RowLayout | None:
    return _record_row_layout(document) or _direct_row_layout(document)


def _sequence_matcher(base: Sequence[object], side: Sequence[object]) -> SequenceMatcher:
    largest = max(len(base), len(side))
    unique_values = len(set(base)) + len(set(side))
    highly_repetitive = largest >= 2_000 and unique_values * 4 < len(base) + len(side)
    return SequenceMatcher(a=base, b=side, autojunk=highly_repetitive)


def _align_rows(base: RowLayout, side: RowLayout) -> RowAlignment:
    base_signatures = [row.signature for row in base.rows]
    side_signatures = [row.signature for row in side.rows]
    matcher = _sequence_matcher(base_signatures, side_signatures)
    mapping: dict[int, int] = {}
    unmatched_base: list[int] = []
    unmatched_side: list[int] = []

    for tag, base_start, base_end, side_start, side_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(base_end - base_start):
                mapping[base_start + offset] = side_start + offset
            continue
        if tag == "replace":
            shared = min(base_end - base_start, side_end - side_start)
            for offset in range(shared):
                mapping[base_start + offset] = side_start + offset
            unmatched_base.extend(range(base_start + shared, base_end))
            unmatched_side.extend(range(side_start + shared, side_end))
            continue
        if tag == "delete":
            unmatched_base.extend(range(base_start, base_end))
        elif tag == "insert":
            unmatched_side.extend(range(side_start, side_end))

    # SequenceMatcher represents a move as one deletion and one insertion.
    # Pair only exact, unique rows; ambiguous duplicate rows remain add/delete
    # operations and therefore cannot be silently attached to the wrong row.
    base_by_signature: dict[tuple[str, ...], list[int]] = {}
    side_by_signature: dict[tuple[str, ...], list[int]] = {}
    for index in unmatched_base:
        base_by_signature.setdefault(base.rows[index].signature, []).append(index)
    for index in unmatched_side:
        side_by_signature.setdefault(side.rows[index].signature, []).append(index)
    moved: set[int] = set()
    paired_base: set[int] = set()
    paired_side: set[int] = set()
    for signature, base_indexes in base_by_signature.items():
        side_indexes = side_by_signature.get(signature, [])
        if len(base_indexes) == len(side_indexes) == 1:
            base_index = base_indexes[0]
            side_index = side_indexes[0]
            mapping[base_index] = side_index
            moved.add(base_index)
            paired_base.add(base_index)
            paired_side.add(side_index)

    inserted = tuple(index for index in unmatched_side if index not in paired_side)
    return RowAlignment(mapping, inserted, frozenset(moved))


def _row_summary(row: RowItem | None) -> str:
    if row is None:
        return "∅ (строки нет)"
    visible = " · ".join(value for value in row.values[:3] if value)
    suffix = f" · {visible}" if visible else ""
    return f"строка {row.coordinate + 1}{suffix}"


def _default_row_choice(states: Mapping[str, str]) -> str | None:
    base_state = states["base"]
    local_changed = states["local"] != base_state
    remote_changed = states["remote"] != base_state
    if local_changed and not remote_changed:
        return "local"
    if remote_changed and not local_changed:
        return "remote"
    if local_changed and remote_changed and states["local"] == states["remote"]:
        return "local"
    return None


def _row_field_anchors(document: MxlDocument, row: RowItem | None) -> list[int]:
    if row is None:
        return []
    anchors: list[int] = []
    ordinal = 0
    tokens = document.tokens
    for index in range(len(tokens) - 4):
        window = tokens[index : index + 5]
        if not (
            window[0].value == "{"
            and window[1].kind == "string"
            and window[1].value == "#"
            and window[2].value == ","
            and window[3].kind == "string"
            and window[4].value == "}"
        ):
            continue
        token_index = index + 3
        if row.start_token <= token_index < row.end_token:
            anchors.append(ordinal)
        ordinal += 1
    return anchors


def _structural_row_conflicts(
    base: MxlDocument, local: MxlDocument, remote: MxlDocument
) -> tuple[RowLayout, RowLayout, RowLayout, tuple[dict[str, object], ...]] | None:
    layouts = (_row_layout(base), _row_layout(local), _row_layout(remote))
    if any(layout is None for layout in layouts):
        return None
    base_layout, local_layout, remote_layout = layouts
    assert base_layout is not None and local_layout is not None and remote_layout is not None
    if not (base_layout.kind == local_layout.kind == remote_layout.kind):
        return None

    local_alignment = _align_rows(base_layout, local_layout)
    remote_alignment = _align_rows(base_layout, remote_layout)
    conflicts: list[dict[str, object]] = []

    def state(row: RowItem | None, moved: bool = False) -> str:
        if row is None:
            return "absent"
        return f"moved:{row.index}" if moved else "present"

    for base_index, base_row in enumerate(base_layout.rows):
        local_index = local_alignment.base_to_side.get(base_index)
        remote_index = remote_alignment.base_to_side.get(base_index)
        local_row = local_layout.rows[local_index] if local_index is not None else None
        remote_row = remote_layout.rows[remote_index] if remote_index is not None else None
        deleted = local_row is None or remote_row is None
        moved_local = base_index in local_alignment.moved
        moved_remote = base_index in remote_alignment.moved
        if not (deleted or moved_local or moved_remote):
            continue
        operation = "delete" if deleted else "move"
        states = {
            "base": state(base_row),
            "local": state(local_row, moved_local),
            "remote": state(remote_row, moved_remote),
        }
        default_choice = _default_row_choice(states)
        if operation == "delete" and (
            (local_row is not None and local_row.values != base_row.values)
            or (remote_row is not None and remote_row.values != base_row.values)
        ):
            # Delete-vs-edit must be an explicit decision: silently preferring
            # the deleting side would discard a real change in the row.
            default_choice = None
        conflicts.append(
            {
                "kind": "row",
                "key": f"row:{operation}:{base_index}",
                "operation": operation,
                "axis": "rows",
                "row_number": base_row.coordinate + 1,
                "base": _row_summary(base_row),
                "local": _row_summary(local_row),
                "remote": _row_summary(remote_row),
                "states": states,
                "default_choice": default_choice,
                "row_coordinates": {
                    "base": base_row.coordinate + 1,
                    "local": local_row.coordinate + 1 if local_row is not None else None,
                    "remote": remote_row.coordinate + 1 if remote_row is not None else None,
                },
                "field_anchors": {
                    "base": _row_field_anchors(base, base_row),
                    "local": _row_field_anchors(local, local_row),
                    "remote": _row_field_anchors(remote, remote_row),
                },
            }
        )

    def insertion_anchor(alignment: RowAlignment, side_index: int) -> int:
        preceding = [
            base_index
            for base_index, mapped_index in alignment.base_to_side.items()
            if mapped_index < side_index
        ]
        return max(preceding, default=-1) + 1

    local_insertions = {
        (local_layout.rows[index].signature, insertion_anchor(local_alignment, index)): index
        for index in local_alignment.inserted
    }
    remote_insertions = {
        (remote_layout.rows[index].signature, insertion_anchor(remote_alignment, index)): index
        for index in remote_alignment.inserted
    }
    insertion_keys = list(dict.fromkeys((*local_insertions, *remote_insertions)))
    for ordinal, insertion_key in enumerate(insertion_keys):
        local_index = local_insertions.get(insertion_key)
        remote_index = remote_insertions.get(insertion_key)
        local_row = local_layout.rows[local_index] if local_index is not None else None
        remote_row = remote_layout.rows[remote_index] if remote_index is not None else None
        states = {
            "base": "absent",
            "local": "present" if local_row is not None else "absent",
            "remote": "present" if remote_row is not None else "absent",
        }
        default_choice = _default_row_choice(states)
        conflicts.append(
            {
                "kind": "row",
                "key": f"row:add:{insertion_key[1]}:{ordinal}",
                "operation": "add",
                "axis": "rows",
                "row_number": (
                    local_row.coordinate + 1
                    if local_row is not None
                    else remote_row.coordinate + 1 if remote_row is not None else None
                ),
                "base": _row_summary(None),
                "local": _row_summary(local_row),
                "remote": _row_summary(remote_row),
                "states": states,
                "default_choice": default_choice,
                "row_coordinates": {
                    "base": None,
                    "local": local_row.coordinate + 1 if local_row is not None else None,
                    "remote": remote_row.coordinate + 1 if remote_row is not None else None,
                },
                "field_anchors": {
                    "base": [],
                    "local": _row_field_anchors(local, local_row),
                    "remote": _row_field_anchors(remote, remote_row),
                },
            }
        )

    if not conflicts:
        return None
    if all(conflict.get("default_choice") is not None for conflict in conflicts):
        defaults_are_compatible = any(
            all(
                isinstance((states := conflict.get("states")), Mapping)
                and states.get(candidate) == states.get(conflict.get("default_choice"))
                for conflict in conflicts
            )
            for candidate in ("base", "local", "remote")
        )
        if not defaults_are_compatible:
            # Individually safe one-sided choices can still form an impossible
            # mixed structure. In that case the complete row layout needs an
            # explicit, consistent decision.
            for conflict in conflicts:
                conflict["default_choice"] = None
    for conflict in conflicts:
        conflict["requires_choice"] = conflict.get("default_choice") is None
    return base_layout, local_layout, remote_layout, tuple(conflicts)


def _sequence_has_order_change(base: Sequence[str], side: Sequence[str]) -> bool:
    """Distinguish equal-length replacements from insert/delete/reorder edits."""
    largest = max(len(base), len(side))
    unique_values = len(set(base)) + len(set(side))
    highly_repetitive = largest >= 2_000 and unique_values * 4 < len(base) + len(side)
    matcher = SequenceMatcher(a=base, b=side, autojunk=highly_repetitive)
    for tag, base_start, base_end, side_start, side_end in matcher.get_opcodes():
        if tag in {"insert", "delete"}:
            return True
        if tag == "replace" and (base_end - base_start) != (side_end - side_start):
            return True
    return False


def _structural_conflict(
    base: MxlDocument,
    local: MxlDocument,
    remote: MxlDocument,
    reason: str,
) -> MergeResult:
    return MergeResult(
        False,
        None,
        reason,
        (
            {
                "kind": "structural",
                "reason": reason,
                "base_token_count": len(base.tokens),
                "local_token_count": len(local.tokens),
                "remote_token_count": len(remote.tokens),
            },
        ),
    )


def _align_entry_indexes(base: Sequence[str], side: Sequence[str]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    matcher = _sequence_matcher(base, side)
    for tag, base_start, base_end, side_start, side_end in matcher.get_opcodes():
        if tag == "equal":
            shared = base_end - base_start
        elif tag == "replace":
            shared = min(base_end - base_start, side_end - side_start)
        else:
            shared = 0
        for offset in range(shared):
            mapping[base_start + offset] = side_start + offset
    return mapping


def _row_mode_value_conflicts(
    base: MxlDocument, local: MxlDocument, remote: MxlDocument
) -> tuple[dict[str, object], ...]:
    base_entries = semantic_entries(base)
    local_entries = semantic_entries(local)
    remote_entries = semantic_entries(remote)
    local_map = _align_entry_indexes(
        [value for _, value in base_entries], [value for _, value in local_entries]
    )
    remote_map = _align_entry_indexes(
        [value for _, value in base_entries], [value for _, value in remote_entries]
    )
    conflicts: list[dict[str, object]] = []
    for entry_index, (base_token_index, base_value) in enumerate(base_entries):
        local_entry = (
            local_entries[local_map[entry_index]] if entry_index in local_map else None
        )
        remote_entry = (
            remote_entries[remote_map[entry_index]] if entry_index in remote_map else None
        )
        local_value = local_entry[1] if local_entry is not None else base_value
        remote_value = remote_entry[1] if remote_entry is not None else base_value
        if local_value in {base_value, remote_value} or remote_value == base_value:
            continue
        conflicts.append(
            {
                "kind": "value",
                "token_index": base_token_index,
                "token_type": base.tokens[base_token_index].kind,
                "base": base_value,
                "local": local_value,
                "remote": remote_value,
                "context": _nearby_strings(base.tokens, base_token_index),
            }
        )
    return tuple(conflicts)


def _selected_row_source(
    row_conflicts: Sequence[Mapping[str, object]],
    resolutions: Mapping[str, Resolution],
) -> str | None:
    unresolved = [
        str(conflict["key"])
        for conflict in row_conflicts
        if str(conflict["key"]) not in resolutions
    ]
    if unresolved:
        return None

    candidates: list[str] = []
    for candidate in ("local", "remote", "base"):
        valid = True
        for conflict in row_conflicts:
            key = str(conflict["key"])
            choice = resolutions[key].get("choice")
            states = conflict.get("states")
            if choice not in {"base", "local", "remote"} or not isinstance(states, Mapping):
                valid = False
                break
            if states.get(candidate) != states.get(choice):
                valid = False
                break
        if valid:
            candidates.append(candidate)
    return candidates[0] if candidates else ""


def _merge_visible_values_into_source(
    base: MxlDocument,
    local: MxlDocument,
    remote: MxlDocument,
    source: MxlDocument,
    resolutions: Mapping[str, Resolution],
) -> MergeResult:
    base_entries = semantic_entries(base)
    local_entries = semantic_entries(local)
    remote_entries = semantic_entries(remote)
    source_entries = semantic_entries(source)
    base_values = [value for _, value in base_entries]
    local_map = _align_entry_indexes(base_values, [value for _, value in local_entries])
    remote_map = _align_entry_indexes(base_values, [value for _, value in remote_entries])
    source_map = _align_entry_indexes(base_values, [value for _, value in source_entries])
    replacements: dict[int, str] = {}
    unresolved: list[dict[str, object]] = []

    for entry_index, (base_token_index, base_value) in enumerate(base_entries):
        if entry_index not in source_map:
            continue
        source_token_index = source_entries[source_map[entry_index]][0]
        local_entry = local_entries[local_map[entry_index]] if entry_index in local_map else None
        remote_entry = remote_entries[remote_map[entry_index]] if entry_index in remote_map else None
        local_value = local_entry[1] if local_entry is not None else base_value
        remote_value = remote_entry[1] if remote_entry is not None else base_value
        resolution = resolutions.get(str(base_token_index))

        if resolution is not None:
            choice = resolution.get("choice")
            if choice == "manual":
                replacements[source_token_index] = _manual_token_value(
                    source.tokens[source_token_index], resolution.get("value", "")
                )
                continue
            selected_entries = {
                "base": (base, (base_token_index, base_value)),
                "local": (local, local_entry),
                "remote": (remote, remote_entry),
            }
            selected = selected_entries.get(str(choice))
            if selected is None:
                return MergeResult(False, None, f"Unknown conflict choice: {choice!r}")
            selected_document, selected_entry = selected
            if selected_entry is None:
                selected_token = base.tokens[base_token_index]
            else:
                selected_token = selected_document.tokens[selected_entry[0]]
        elif local_value == remote_value:
            selected_token = (
                local.tokens[local_entry[0]] if local_entry is not None else base.tokens[base_token_index]
            )
        elif local_value == base_value:
            selected_token = (
                remote.tokens[remote_entry[0]] if remote_entry is not None else base.tokens[base_token_index]
            )
        elif remote_value == base_value:
            selected_token = (
                local.tokens[local_entry[0]] if local_entry is not None else base.tokens[base_token_index]
            )
        else:
            unresolved.append(
                {
                    "kind": "value",
                    "token_index": base_token_index,
                    "base": base_value,
                    "local": local_value,
                    "remote": remote_value,
                }
            )
            continue

        if selected_token.raw != source.tokens[source_token_index].raw:
            replacements[source_token_index] = selected_token.raw

    if unresolved:
        return MergeResult(
            False,
            None,
            f"{len(unresolved)} MXL conflict(s) have not been resolved",
            tuple(unresolved),
        )
    return MergeResult(
        True,
        _replace_tokens(source, replacements),
        f"Selected the {Path(source.path).name or source.path} row structure and merged "
        f"{len(replacements)} visible value change(s)",
    )


def _scope_semantic_sequence(
    document: MxlDocument, scope: tuple[int, int]
) -> tuple[tuple[str, str], ...]:
    start, end = scope
    return tuple(token.semantic_value for token in document.tokens[start:end])


def _other_side_changed_scope(
    base: MxlDocument, other: MxlDocument, token_index: int
) -> bool:
    base_scope = base.scopes[token_index]
    other_scope = other.scopes[token_index]
    if (base_scope[1] - base_scope[0]) != (other_scope[1] - other_scope[0]):
        return True
    return _scope_semantic_sequence(base, base_scope) != _scope_semantic_sequence(
        other, other_scope
    )


def merge_documents(base: MxlDocument, local: MxlDocument, remote: MxlDocument) -> MergeResult:
    if local.data == remote.data:
        return MergeResult(True, local.data, "Both sides are byte-for-byte identical")
    if local.data == base.data:
        return MergeResult(True, remote.data, "Only the remote side changed")
    if remote.data == base.data:
        return MergeResult(True, local.data, "Only the local side changed")
    if local.semantic_sequence == remote.semantic_sequence:
        return MergeResult(True, local.data, "Both sides are semantically identical")

    row_analysis = _structural_row_conflicts(base, local, remote)
    if row_analysis is not None:
        _, _, _, row_conflicts = row_analysis
        value_conflicts = _row_mode_value_conflicts(base, local, remote)
        return MergeResult(
            False,
            None,
            "Spreadsheet rows were added, removed, or reordered",
            row_conflicts + value_conflicts,
        )

    structures_match = (
        base.structural_sequence == local.structural_sequence == remote.structural_sequence
    )
    if not structures_match:
        if local.semantic_sequence == base.semantic_sequence:
            return MergeResult(True, remote.data, "Only the remote side changed semantically")
        if remote.semantic_sequence == base.semantic_sequence:
            return MergeResult(True, local.data, "Only the local side changed semantically")
        return _structural_conflict(
            base,
            local,
            remote,
            "Both sides changed the serialized MXL structure",
        )

    base_visible = _visible_values_from_tokens(base.tokens)
    if _sequence_has_order_change(base_visible, _visible_values_from_tokens(local.tokens)) or (
        _sequence_has_order_change(base_visible, _visible_values_from_tokens(remote.tokens))
    ):
        return _structural_conflict(
            base,
            local,
            remote,
            "At least one side inserted, deleted, or reordered visible MXL fields",
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
            if _other_side_changed_scope(base, local, index):
                return _structural_conflict(
                    base,
                    local,
                    remote,
                    "A remote value cannot be matched safely after local changes in its container",
                )
            replacements[index] = remote_token.raw
            continue
        if remote_value == base_value:
            if _other_side_changed_scope(base, remote, index):
                return _structural_conflict(
                    base,
                    local,
                    remote,
                    "A local value cannot be matched safely after remote changes in its container",
                )
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

    Value-conflict keys are token indexes converted to strings. Row-operation
    keys begin with ``row:`` and select the Base/Local/Remote state of that
    operation. A non-tabular structural conflict uses the special
    ``structural`` key and accepts a whole-file Base/Local/Remote choice. The
    special ``document`` key always selects one complete input document.
    """

    document_resolution = resolutions.get("document")
    if document_resolution is not None:
        choice = document_resolution.get("choice")
        documents = {"base": base, "local": local, "remote": remote}
        if choice not in documents:
            return MergeResult(False, None, f"Unsupported whole-document choice: {choice!r}")
        selected = documents[str(choice)]
        return MergeResult(True, selected.data, f"Selected the complete {choice} MXL document")

    automatic_result = merge_documents(base, local, remote)
    if automatic_result.success and not resolutions:
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

    row_conflicts = [
        conflict for conflict in automatic_result.conflicts if conflict["kind"] == "row"
    ]
    if row_conflicts:
        source_name = _selected_row_source(row_conflicts, resolutions)
        if source_name is None:
            unresolved = sum(
                1 for conflict in row_conflicts if str(conflict["key"]) not in resolutions
            )
            return MergeResult(
                False,
                None,
                f"{unresolved} row operation(s) have not been resolved",
                tuple(row_conflicts),
            )
        if not source_name:
            return MergeResult(
                False,
                None,
                "This combination of row decisions does not match a complete Base, "
                "Local, or Remote structure. Choose one consistent structure so MXL "
                "ranges and formatting references remain valid.",
                tuple(row_conflicts),
            )
        documents = {"base": base, "local": local, "remote": remote}
        return _merge_visible_values_into_source(
            base, local, remote, documents[source_name], resolutions
        )

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
        resolution = resolutions.get(str(index))

        if resolution is not None:
            replacement = _resolved_token_raw(
                resolution, base_token, local_token, remote_token
            )
            if replacement != local_token.raw:
                replacements[index] = replacement
            resolved_count += 1
            continue

        if local_value == remote_value:
            continue
        if local_value == base_value:
            replacements[index] = remote_token.raw
            continue
        if remote_value == base_value:
            continue

        unresolved.append(
            {
                "kind": "value",
                "token_index": index,
                "base": _display_token(base_token),
                "local": _display_token(local_token),
                "remote": _display_token(remote_token),
            }
        )

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


def semantic_coordinates(document: MxlDocument) -> list[str | None]:
    """Return R#C# coordinates aligned with :func:`semantic_entries`.

    Normal MOXCEL row records store zero-based row and column coordinates.
    Simple direct-row documents used by lightweight fixtures fall back to one
    logical column per semantic row.
    """

    entries = semantic_entries(document)
    entry_token_indexes = [token_index for token_index, _value in entries]
    coordinates_by_token: dict[int, str] = {}
    layout = _row_layout(document)
    if layout is not None and layout.kind == "records":
        children = document.root.children
        for row in layout.rows:
            cell_children = children[row.start_child + 4 : row.end_child]
            for offset in range(0, len(cell_children), 2):
                cell = cell_children[offset]
                if not isinstance(cell, StructureNode):
                    continue
                if offset == 0:
                    column = 0
                else:
                    column_child = cell_children[offset - 1]
                    parsed_column = _integer_child(document, column_child)
                    if parsed_column is None:
                        continue
                    column = parsed_column
                entry_index = bisect_left(entry_token_indexes, cell.start)
                while (
                    entry_index < len(entry_token_indexes)
                    and entry_token_indexes[entry_index] < cell.end
                ):
                    coordinates_by_token[entry_token_indexes[entry_index]] = (
                        f"R{row.coordinate + 1}C{column + 1}"
                    )
                    entry_index += 1
    elif layout is not None:
        for row in layout.rows:
            entry_index = bisect_left(entry_token_indexes, row.start_token)
            while (
                entry_index < len(entry_token_indexes)
                and entry_token_indexes[entry_index] < row.end_token
            ):
                coordinates_by_token[entry_token_indexes[entry_index]] = (
                    f"R{row.coordinate + 1}C1"
                )
                entry_index += 1

    return [
        coordinates_by_token.get(token_index)
        for token_index, _value in entries
    ]


def semantic_values(document: MxlDocument) -> list[str]:
    return [value for _, value in semantic_entries(document)]


def textconv(document: MxlDocument) -> str:
    """Return a complete, whitespace-independent canonical token stream."""
    lines = ["# MXL canonical semantic token stream"]
    for token in document.tokens:
        if token.kind == "string":
            lines.append("S " + json.dumps(token.value, ensure_ascii=False))
        elif token.kind == "atom":
            lines.append("A " + token.semantic_value[1])
        else:
            lines.append("P " + token.value)
    return "\n".join(lines) + "\n"


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Durably replace a file without exposing a partially written result."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if existing_mode is not None:
            temporary.chmod(existing_mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def _canonical_repository_path(target_path: str | Path) -> str:
    try:
        root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        ).resolve()
        target = Path(target_path)
        absolute = (Path.cwd() / target).resolve() if not target.is_absolute() else target.resolve()
        return absolute.relative_to(root).as_posix()
    except (OSError, ValueError, subprocess.CalledProcessError):
        return str(Path(target_path))


def driver_report_path(target_path: str | Path) -> Path:
    """Keep merge diagnostics inside Git metadata rather than the worktree."""
    canonical = _canonical_repository_path(target_path)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    basename = Path(canonical).name or "mxl"
    try:
        git_directory = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--absolute-git-dir"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_directory = Path.cwd() / ".git"
    return git_directory / "mxl-merge" / "reports" / f"{digest}-{basename}.json"


def _write_report(report_path: Path, target_path: str, result: MergeResult) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "path": target_path,
        "reason": result.reason,
        "conflicts": result.conflicts,
    }
    atomic_write_text(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )


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
    parse_document(result.data, output_path)
    atomic_write_bytes(output_path, result.data)
    _remove_stale_report(report)
    print(f"MXL merge: {result.reason}", file=sys.stderr)
    return 0


def install_git_config(
    onec_client: str | None = None,
    onec_infobase: str | None = None,
    onec_epf: str | None = None,
    onec_username: str | None = None,
    onec_batch_capable: bool | None = None,
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
    # The version is part of Git's textconv cache key. Bump it whenever the
    # canonical output changes so reinstalling cannot reuse stale blob output.
    textconv_command = (
        f'{python_command} textconv --format-version {TEXTCONV_FORMAT_VERSION}'
    )
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
        batch_capable = epf_supports_batch(epf_path, explicit=onec_batch_capable)
        subprocess.run(
            [
                "git",
                "config",
                config_scope,
                "mxl.onecBatchCapable",
                "true" if batch_capable else "false",
            ],
            check=True,
        )
        if batch_capable:
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
        else:
            subprocess.run(
                [
                    "git",
                    "config",
                    config_scope,
                    "--unset-all",
                    "mxl.previewBatchCommand",
                ],
                check=False,
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
    atomic_write_text(path, f"{text}{separator}{MXL_ATTRIBUTES_LINE}\n")


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
    textconv_parser.add_argument(
        "--format-version",
        choices=[TEXTCONV_FORMAT_VERSION],
        default=TEXTCONV_FORMAT_VERSION,
        help=argparse.SUPPRESS,
    )

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
        "--onec-batch-capable",
        action="store_true",
        default=None,
        help="Declare that the configured EPF supports the batch manifest protocol",
    )
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
            report = str(driver_report_path(args.path))
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
                args.onec_batch_capable,
                args.global_install,
            )
    except (OSError, MxlFormatError, subprocess.CalledProcessError) as error:
        print(f"mxl-tool: {error}", file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
