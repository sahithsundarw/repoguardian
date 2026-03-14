"""
AST-based code symbol extractor using tree-sitter.

Given a file's source code and a set of line numbers (from the diff),
this module identifies which functions/classes/methods contain those
lines and returns their full source definitions.

This gives agents the complete function body rather than just the changed
lines — essential for correct reasoning about bugs and security issues.

Supported languages (Tier 1):
  python, javascript, typescript, java, go
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

from backend.models.schemas import ChangedSymbol

logger = logging.getLogger(__name__)

Language = Literal["python", "javascript", "typescript", "java", "go", "unknown"]

# ── Language detection ─────────────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, Language] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
}


def detect_language(file_path: str) -> Language:
    """Detect programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext, "unknown")


# ── Tree-sitter parser cache ───────────────────────────────────────────────────

_parser_cache: dict[Language, Any] = {}


def _get_parser(language: Language) -> Any | None:
    """Return a cached tree-sitter parser for the given language, or None."""
    if language in _parser_cache:
        return _parser_cache[language]

    try:
        import tree_sitter_python as tspython
        import tree_sitter_javascript as tsjavascript
        import tree_sitter_typescript as tstypescript
        import tree_sitter_java as tsjava
        import tree_sitter_go as tsgo
        from tree_sitter import Language as TSLanguage, Parser

        lang_map = {
            "python": tspython.language(),
            "javascript": tsjavascript.language(),
            "typescript": tstypescript.language_typescript(),
            "java": tsjava.language(),
            "go": tsgo.language(),
        }

        if language not in lang_map:
            _parser_cache[language] = None
            return None

        ts_lang = TSLanguage(lang_map[language])
        parser = Parser(ts_lang)
        _parser_cache[language] = parser
        return parser
    except ImportError as e:
        logger.warning("tree-sitter not available for %s: %s", language, e)
        _parser_cache[language] = None
        return None


# ── Query strings per language ─────────────────────────────────────────────────

# tree-sitter S-expression queries to find function/class/method nodes
_QUERIES: dict[Language, str] = {
    "python": """
        (function_definition name: (identifier) @func.name) @func.def
        (class_definition name: (identifier) @class.name) @class.def
    """,
    "javascript": """
        (function_declaration name: (identifier) @func.name) @func.def
        (method_definition name: (property_identifier) @func.name) @func.def
        (arrow_function) @func.def
        (class_declaration name: (identifier) @class.name) @class.def
    """,
    "typescript": """
        (function_declaration name: (identifier) @func.name) @func.def
        (method_definition name: (property_identifier) @func.name) @func.def
        (class_declaration name: (identifier) @class.name) @class.def
    """,
    "java": """
        (method_declaration name: (identifier) @func.name) @func.def
        (class_declaration name: (identifier) @class.name) @class.def
    """,
    "go": """
        (function_declaration name: (identifier) @func.name) @func.def
        (method_declaration name: (field_identifier) @func.name) @func.def
    """,
}


# ── Public API ─────────────────────────────────────────────────────────────────


def extract_symbols_at_lines(
    source_code: str,
    file_path: str,
    target_lines: list[int],
) -> list[ChangedSymbol]:
    """
    Given source code and a list of changed line numbers (1-indexed),
    return the enclosing function/class symbols.

    Falls back to line-range heuristics if tree-sitter is unavailable.
    """
    language = detect_language(file_path)
    parser = _get_parser(language)

    if parser is not None:
        return _extract_with_treesitter(source_code, file_path, target_lines, language, parser)
    else:
        return _extract_with_heuristics(source_code, file_path, target_lines, language)


def extract_all_symbols(
    source_code: str,
    file_path: str,
) -> list[ChangedSymbol]:
    """Extract all top-level function/class symbols from a file."""
    language = detect_language(file_path)
    parser = _get_parser(language)
    lines = source_code.splitlines()
    all_lines = list(range(1, len(lines) + 1))

    if parser is not None:
        return _extract_with_treesitter(source_code, file_path, all_lines, language, parser)
    return []


def get_function_source(
    source_code: str,
    file_path: str,
    function_name: str,
) -> str | None:
    """
    Return the full source of a named function/method from a file.
    Returns None if not found.
    """
    symbols = extract_all_symbols(source_code, file_path)
    for sym in symbols:
        if sym.name == function_name:
            return sym.full_source
    return None


# ── Tree-sitter implementation ─────────────────────────────────────────────────


def _extract_with_treesitter(
    source_code: str,
    file_path: str,
    target_lines: list[int],
    language: Language,
    parser: Any,
) -> list[ChangedSymbol]:
    """Use tree-sitter to find symbols enclosing the target lines."""
    target_set = set(target_lines)
    source_bytes = source_code.encode("utf-8")

    try:
        tree = parser.parse(source_bytes)
    except Exception as e:
        logger.error("tree-sitter parse error for %s: %s", file_path, e)
        return []

    symbols: list[ChangedSymbol] = []
    seen_ranges: set[tuple[int, int]] = set()

    def visit(node: Any) -> None:
        """Recursively visit nodes looking for function/class definitions."""
        node_kind = node.type
        is_def = any(
            k in node_kind
            for k in ("function", "method", "class", "def")
        )

        if is_def:
            start_line = node.start_point[0] + 1  # tree-sitter is 0-indexed
            end_line = node.end_point[0] + 1
            range_key = (start_line, end_line)

            # Check if any target line falls within this symbol
            overlaps = any(start_line <= ln <= end_line for ln in target_set)
            if overlaps and range_key not in seen_ranges:
                seen_ranges.add(range_key)
                name = _extract_name(node)
                kind = _classify_node(node_kind)
                src_lines = source_code.splitlines()[start_line - 1: end_line]
                symbols.append(ChangedSymbol(
                    name=name or f"anonymous@{start_line}",
                    kind=kind,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    full_source="\n".join(src_lines),
                ))

        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return symbols


def _extract_name(node: Any) -> str | None:
    """Extract the name child from a definition node."""
    for child in node.children:
        if child.type in ("identifier", "property_identifier", "field_identifier"):
            return child.text.decode("utf-8") if child.text else None
    return None


def _classify_node(node_type: str) -> str:
    if "class" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    if "function" in node_type or "def" in node_type:
        return "function"
    return "variable"


# ── Heuristic fallback ─────────────────────────────────────────────────────────

_PY_DEF = re.compile(r"^(def |class |async def )")
_JS_DEF = re.compile(r"^(function |class |const \w+ = (async )?\(|(async )?function\*? \w+)")


def _extract_with_heuristics(
    source_code: str,
    file_path: str,
    target_lines: list[int],
    language: Language,
) -> list[ChangedSymbol]:
    """
    Regex-based fallback for languages without tree-sitter support.
    Less accurate but ensures some context is always provided.
    """
    if language not in ("python", "javascript", "typescript"):
        return []

    lines = source_code.splitlines()
    target_set = set(target_lines)
    symbols: list[ChangedSymbol] = []

    # Find all definition start points
    def_starts: list[int] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if language == "python" and _PY_DEF.match(stripped):
            def_starts.append(i)
        elif language in ("javascript", "typescript") and _JS_DEF.match(stripped):
            def_starts.append(i)

    # For each target line, find the enclosing definition
    for tl in target_set:
        enclosing_start = None
        for ds in reversed(def_starts):
            if ds <= tl:
                enclosing_start = ds
                break
        if enclosing_start is None:
            continue

        # Find end: next definition at same indentation or EOF
        indent = len(lines[enclosing_start - 1]) - len(lines[enclosing_start - 1].lstrip())
        end_line = len(lines)
        for j in range(enclosing_start, len(lines)):
            line = lines[j]
            if j > enclosing_start - 1 and line.strip():
                curr_indent = len(line) - len(line.lstrip())
                if curr_indent <= indent and any(
                    kw in line for kw in ("def ", "class ", "function ", "const ")
                ):
                    end_line = j
                    break

        src = "\n".join(lines[enclosing_start - 1: end_line])
        name_match = re.search(r"(?:def |function |class )(\w+)", lines[enclosing_start - 1])
        name = name_match.group(1) if name_match else f"symbol@{enclosing_start}"

        symbols.append(ChangedSymbol(
            name=name,
            kind="function",
            file_path=file_path,
            start_line=enclosing_start,
            end_line=end_line,
            full_source=src,
        ))

    return symbols


