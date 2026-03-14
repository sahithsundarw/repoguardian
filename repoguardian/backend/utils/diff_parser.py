"""
Unified diff parser.

Parses raw unified diff text (as returned by GitHub's API or `git diff`) into
structured DiffHunk objects, and identifies the changed files and line ranges
so downstream agents can focus on relevant code locations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Generator

from backend.models.schemas import DiffHunk


# ── Internal raw hunk dataclass (before Pydantic conversion) ──────────────────


@dataclass
class RawHunk:
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    raw_lines: list[str] = field(default_factory=list)


# ── Regex patterns ─────────────────────────────────────────────────────────────

_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# ── Public API ─────────────────────────────────────────────────────────────────


def parse_diff(raw_diff: str) -> list[DiffHunk]:
    """
    Parse a unified diff string into a list of DiffHunk objects.

    Args:
        raw_diff: Raw unified diff text, e.g. from GitHub compare API.

    Returns:
        List of DiffHunk objects, one per hunk in the diff.
    """
    if not raw_diff or not raw_diff.strip():
        return []

    raw_hunks = list(_iter_raw_hunks(raw_diff))
    return [_to_diff_hunk(rh) for rh in raw_hunks]


def get_changed_files(diff_hunks: list[DiffHunk]) -> list[str]:
    """Return deduplicated list of file paths touched by the diff."""
    seen: set[str] = set()
    result: list[str] = []
    for hunk in diff_hunks:
        if hunk.file_path not in seen:
            seen.add(hunk.file_path)
            result.append(hunk.file_path)
    return result


def get_changed_line_ranges(diff_hunks: list[DiffHunk]) -> dict[str, list[tuple[int, int]]]:
    """
    Return a mapping of file_path → list of (start_line, end_line) tuples
    representing the new-file line ranges that were added or modified.
    Used by agents to focus their analysis on changed sections.
    """
    result: dict[str, list[tuple[int, int]]] = {}
    for hunk in diff_hunks:
        ranges = result.setdefault(hunk.file_path, [])
        ranges.append((hunk.new_start, hunk.new_start + max(hunk.new_count - 1, 0)))
    return result


def summarize_diff(diff_hunks: list[DiffHunk]) -> dict[str, Any]:
    """
    Compute a quick statistical summary of the diff for the orchestrator.
    """
    total_added = sum(len(h.added_lines) for h in diff_hunks)
    total_removed = sum(len(h.removed_lines) for h in diff_hunks)
    return {
        "files_changed": len(get_changed_files(diff_hunks)),
        "hunks": len(diff_hunks),
        "lines_added": total_added,
        "lines_removed": total_removed,
        "net_change": total_added - total_removed,
    }


# ── Private helpers ────────────────────────────────────────────────────────────


def _iter_raw_hunks(raw_diff: str) -> Generator[RawHunk, None, None]:
    """Yield RawHunk objects by scanning the diff line by line."""
    current_file: str | None = None
    current_hunk: RawHunk | None = None

    for line in raw_diff.splitlines():
        # Detect new file header (+++ b/path)
        file_match = _FILE_HEADER.match(line)
        if file_match:
            if current_hunk is not None:
                yield current_hunk
                current_hunk = None
            current_file = file_match.group(1)
            continue

        # Detect hunk header (@@ -a,b +c,d @@)
        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match and current_file is not None:
            if current_hunk is not None:
                yield current_hunk
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2) or 1)
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4) or 1)
            current_hunk = RawHunk(
                file_path=current_file,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )
            continue

        # Accumulate hunk lines
        if current_hunk is not None and line and line[0] in ("+", "-", " "):
            current_hunk.raw_lines.append(line)

    if current_hunk is not None:
        yield current_hunk


def _to_diff_hunk(raw: RawHunk) -> DiffHunk:
    """Convert a RawHunk into the Pydantic DiffHunk schema."""
    added = [line[1:] for line in raw.raw_lines if line.startswith("+")]
    removed = [line[1:] for line in raw.raw_lines if line.startswith("-")]
    context = [line[1:] for line in raw.raw_lines if line.startswith(" ")]

    return DiffHunk(
        file_path=raw.file_path,
        old_start=raw.old_start,
        old_count=raw.old_count,
        new_start=raw.new_start,
        new_count=raw.new_count,
        lines=raw.raw_lines,
        context_lines=context,
        added_lines=added,
        removed_lines=removed,
    )


