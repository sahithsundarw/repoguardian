"""
Context Retrieval Agent.

Assembles the ContextPackage that all specialist agents receive.
This agent runs synchronously BEFORE any parallel analysis begins.

Assembly strategy (in priority order):
  1. Parse raw diff → DiffHunks (always included, never truncated)
  2. AST-expand changed symbols → full function/class definitions
  3. Collect call graph neighbours (1 hop: callers + callees)
  4. Fetch related test files
  5. Semantic similarity search via ChromaDB
  6. Dependency manifests
  7. Documentation files (README, docstrings)
  8. Repository structure summary (tree depth=2)

Each section is token-budget-constrained. Lower-priority sections
are dropped or truncated if the budget is exhausted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from backend.agents.base import BaseAgent
from backend.config import get_settings
from backend.models.schemas import (
    CallGraphEdge,
    ChangedSymbol,
    ContextPackage,
    FileContent,
    SimilarChunk,
    WebhookEvent,
)
from backend.services.git_service import GitHubDiffFetcher, RepoContext
from backend.services.vector_store import search_similar
from backend.utils.ast_extractor import extract_symbols_at_lines, detect_language
from backend.utils.diff_parser import (
    get_changed_files,
    get_changed_line_ranges,
    parse_diff,
)
from backend.utils.token_counter import ContextBudgetManager, count_tokens

logger = logging.getLogger(__name__)
settings = get_settings()


class ContextRetrievalAgent(BaseAgent):
    """
    Assembles the shared ContextPackage for all specialist agents.
    Does NOT call the LLM — pure code analysis and retrieval.
    """

    name = "context_retrieval"

    def __init__(self, github_token: str = "") -> None:
        super().__init__()
        self._github_token = github_token or settings.github_token
        self._diff_fetcher = GitHubDiffFetcher(self._github_token)

    async def run(self, event: WebhookEvent, repo_id: str) -> ContextPackage:
        """
        Build the full ContextPackage for the given webhook event.

        Args:
            event:    The normalised webhook event.
            repo_id:  Database UUID of the repository.

        Returns:
            A fully assembled ContextPackage.
        """
        self.log_info("Starting context assembly for %s PR#%s",
                      event.repo_full_name, event.pr_number)

        budget = ContextBudgetManager(total_budget=settings.context_total_token_budget)

        # ── Step 1: Fetch and parse diff ───────────────────────────────────────
        raw_diff = await self._fetch_diff(event)
        raw_diff = budget.allocate(
            "diff", raw_diff,
            max_tokens=settings.context_diff_max_tokens,
            required=True,  # never truncate the diff
        )
        diff_hunks = parse_diff(raw_diff)
        changed_files = get_changed_files(diff_hunks)
        changed_line_ranges = get_changed_line_ranges(diff_hunks)

        self.log_info("Diff parsed: %d files, %d hunks", len(changed_files), len(diff_hunks))

        # ── Step 2: AST expansion ──────────────────────────────────────────────
        changed_symbols, expanded_definitions = await self._expand_symbols(
            event, changed_files, changed_line_ranges, budget
        )

        # ── Step 3: Call graph neighbours ─────────────────────────────────────
        call_graph_edges, callers, callees = await self._build_call_graph(
            event, changed_symbols, changed_files, budget
        )

        # ── Step 4: Test files ─────────────────────────────────────────────────
        test_files = await self._fetch_test_files(event, changed_files, budget)

        # ── Step 5: Semantic similarity ────────────────────────────────────────
        semantic_neighbors = await self._semantic_search(raw_diff, repo_id, budget)

        # ── Step 6: Dependency manifests ──────────────────────────────────────
        dep_manifests = await self._fetch_manifests(event, budget)

        # ── Step 7: Documentation ─────────────────────────────────────────────
        doc_files = await self._fetch_docs(event, budget)

        # ── Step 8: Repository structure ──────────────────────────────────────
        repo_structure = await self._fetch_repo_structure(event, budget)

        token_summary = budget.summary()
        self.log_info("Context assembled: %d tokens used / %d budget",
                      token_summary["_total_used"], token_summary["_total_budget"])

        return ContextPackage(
            repo_id=repo_id,
            repo_full_name=event.repo_full_name,
            event_type=event.event_type,
            pr_number=event.pr_number,
            pr_title=event.pr_title,
            pr_description=event.pr_description,
            pr_author=event.pr_author,
            raw_diff=raw_diff,
            diff_hunks=diff_hunks,
            changed_files=changed_files,
            changed_symbols=changed_symbols,
            expanded_definitions=expanded_definitions,
            call_graph_edges=call_graph_edges,
            callers=callers,
            callees=callees,
            relevant_test_files=test_files,
            semantic_neighbors=semantic_neighbors,
            dependency_manifests=dep_manifests,
            documentation_files=doc_files,
            repo_structure=repo_structure,
            total_tokens_used=token_summary["_total_used"],
            budget_remaining=token_summary["_remaining"],
        )

    # ── Step implementations ───────────────────────────────────────────────────

    async def _fetch_diff(self, event: WebhookEvent) -> str:
        """Fetch the PR diff from GitHub API or from the event payload."""
        if event.pr_number and event.repo_full_name:
            owner, repo_name = event.repo_full_name.split("/", 1)
            try:
                return await self._diff_fetcher.fetch_pr_diff(owner, repo_name, event.pr_number)
            except Exception as e:
                logger.warning("GitHub diff fetch failed, using raw_payload diff: %s", e)

        # Fall back to diff embedded in the payload (if any)
        return event.raw_payload.get("diff", "")

    async def _expand_symbols(
        self,
        event: WebhookEvent,
        changed_files: list[str],
        changed_line_ranges: dict[str, list[tuple[int, int]]],
        budget: ContextBudgetManager,
    ) -> tuple[list[ChangedSymbol], dict[str, str]]:
        """
        For each changed file, use the AST extractor to pull the full
        definitions of functions/classes containing the changed lines.
        """
        all_symbols: list[ChangedSymbol] = []
        expanded: dict[str, str] = {}
        combined_defs = []

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"

        # Fetch each changed file concurrently (up to 10 at a time)
        sem = asyncio.Semaphore(10)

        async def process_file(file_path: str) -> None:
            async with sem:
                language = detect_language(file_path)
                if language == "unknown":
                    return

                source = await self._diff_fetcher.fetch_file_content(
                    owner, repo_name, file_path, ref
                )
                if not source:
                    return

                ranges = changed_line_ranges.get(file_path, [])
                target_lines = []
                for start, end in ranges:
                    target_lines.extend(range(start, end + 1))

                symbols = extract_symbols_at_lines(source, file_path, target_lines)
                all_symbols.extend(symbols)

                for sym in symbols:
                    key = f"{sym.file_path}::{sym.name}"
                    if key not in expanded:
                        expanded[key] = sym.full_source
                        combined_defs.append(f"# {key}\n{sym.full_source}")

        await asyncio.gather(*[process_file(f) for f in changed_files[:20]])

        defs_text = "\n\n".join(combined_defs)
        budget.allocate(
            "expanded_definitions",
            defs_text,
            max_tokens=settings.context_definitions_max_tokens,
        )

        self.log_info("AST expansion: %d symbols from %d files",
                      len(all_symbols), len(changed_files))
        return all_symbols, expanded

    async def _build_call_graph(
        self,
        event: WebhookEvent,
        symbols: list[ChangedSymbol],
        changed_files: list[str],
        budget: ContextBudgetManager,
    ) -> tuple[list[CallGraphEdge], dict[str, list[str]], dict[str, list[str]]]:
        """
        Lightweight call graph: find callers of changed symbols via GitHub search.
        Returns edges, callers dict, callees dict.
        """
        if not symbols or budget.remaining < 1000:
            return [], {}, {}

        edges: list[CallGraphEdge] = []
        callers: dict[str, list[str]] = {}
        callees: dict[str, list[str]] = {}
        combined_cg = []

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"

        # For each symbol, search for its usages in other files
        for sym in symbols[:5]:  # Limit to top 5 symbols for cost control
            callers[sym.name] = []

            # Use GitHub code search API to find callers (simplified)
            # In production, this would use a pre-built call graph index
            # Here we simulate with a file fetch of callee's own file to get
            # imports and docstrings
            try:
                source = await self._diff_fetcher.fetch_file_content(
                    owner, repo_name, sym.file_path, ref
                )
                if source:
                    callees_text = self._extract_callees_from_source(source, sym.name)
                    callees[sym.name] = callees_text
                    combined_cg.append(
                        f"# Callees of {sym.name} in {sym.file_path}\n"
                        + "\n".join(callees_text[:3])
                    )
            except Exception as e:
                logger.debug("Call graph extraction failed for %s: %s", sym.name, e)

        cg_text = "\n\n".join(combined_cg)
        budget.allocate("call_graph", cg_text, max_tokens=settings.context_call_graph_max_tokens)

        return edges, callers, callees

    def _extract_callees_from_source(self, source: str, function_name: str) -> list[str]:
        """
        Extract function calls made within `function_name`'s body.
        Simple regex-based extraction.
        """
        import re
        # Find the function body
        pattern = re.compile(
            rf"(def\s+{re.escape(function_name)}\s*\([^)]*\)[^:]*:)(.*?)(?=\ndef\s|\nclass\s|\Z)",
            re.DOTALL
        )
        match = pattern.search(source)
        if not match:
            return []

        body = match.group(2)
        # Find all function calls (word followed by open paren)
        calls = re.findall(r"\b(\w+)\s*\(", body)
        # Filter out common keywords
        keywords = {"if", "for", "while", "print", "return", "len", "range", "str", "int"}
        return [c for c in dict.fromkeys(calls) if c not in keywords][:10]

    async def _fetch_test_files(
        self,
        event: WebhookEvent,
        changed_files: list[str],
        budget: ContextBudgetManager,
    ) -> list[FileContent]:
        """Fetch test files that relate to the changed source files."""
        if budget.remaining < 2000:
            return []

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"
        test_files: list[FileContent] = []
        combined = []

        for source_file in changed_files[:5]:
            stem = Path(source_file).stem
            # Common test file naming conventions
            candidates = [
                f"tests/test_{stem}.py",
                f"tests/{stem}_test.py",
                f"test/{stem}.test.js",
                f"__tests__/{stem}.test.ts",
                f"spec/{stem}_spec.rb",
            ]
            for candidate in candidates:
                content = await self._diff_fetcher.fetch_file_content(
                    owner, repo_name, candidate, ref
                )
                if content:
                    tc = count_tokens(content)
                    test_files.append(FileContent(path=candidate, content=content, token_count=tc))
                    combined.append(f"# {candidate}\n{content}")
                    break  # One test file per source file is enough

        test_text = "\n\n".join(combined)
        budget.allocate("test_files", test_text, max_tokens=settings.context_tests_max_tokens)
        return test_files

    async def _semantic_search(
        self,
        diff_text: str,
        repo_id: str,
        budget: ContextBudgetManager,
    ) -> list[SimilarChunk]:
        """Search for semantically similar code in the vector store."""
        if budget.remaining < 1000:
            return []

        try:
            neighbors = search_similar(
                query_text=diff_text[:2000],  # Use first 2K chars as query
                repo_id=repo_id,
                top_k=5,
                min_similarity=0.65,
            )
            combined = "\n\n".join(
                f"# Similar: {n.file_path}:{n.start_line} (score={n.similarity_score})\n{n.source}"
                for n in neighbors
            )
            budget.allocate(
                "semantic_neighbors", combined,
                max_tokens=settings.context_semantic_max_tokens,
            )
            return neighbors
        except Exception as e:
            logger.warning("Semantic search failed: %s", e)
            return []

    async def _fetch_manifests(
        self,
        event: WebhookEvent,
        budget: ContextBudgetManager,
    ) -> list[FileContent]:
        """Fetch dependency manifest files."""
        if budget.remaining < 500:
            return []

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"
        manifest_names = [
            "requirements.txt", "pyproject.toml", "package.json",
            "Cargo.toml", "go.mod", "pom.xml",
        ]
        manifests: list[FileContent] = []
        combined = []

        for name in manifest_names:
            content = await self._diff_fetcher.fetch_file_content(
                owner, repo_name, name, ref
            )
            if content:
                manifests.append(FileContent(path=name, content=content))
                combined.append(f"# {name}\n{content}")

        budget.allocate(
            "manifests", "\n\n".join(combined),
            max_tokens=settings.context_manifests_max_tokens,
        )
        return manifests

    async def _fetch_docs(
        self,
        event: WebhookEvent,
        budget: ContextBudgetManager,
    ) -> list[FileContent]:
        """Fetch documentation files (README, CHANGELOG, etc.)."""
        if budget.remaining < 300:
            return []

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"
        doc_candidates = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "docs/index.md"]
        docs: list[FileContent] = []
        combined = []

        for name in doc_candidates:
            content = await self._diff_fetcher.fetch_file_content(
                owner, repo_name, name, ref
            )
            if content:
                docs.append(FileContent(path=name, content=content[:3000]))
                combined.append(f"# {name}\n{content[:3000]}")

        budget.allocate(
            "documentation", "\n\n".join(combined),
            max_tokens=settings.context_docs_max_tokens,
        )
        return docs

    async def _fetch_repo_structure(
        self,
        event: WebhookEvent,
        budget: ContextBudgetManager,
    ) -> str:
        """Build a lightweight directory tree of the repository."""
        if budget.remaining < 200:
            return ""

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.head_sha or "HEAD"

        try:
            tree_files = await self._diff_fetcher.get_repo_tree(owner, repo_name, ref)
            # Build a pseudo-tree from the flat file list (depth 2)
            dirs: set[str] = set()
            for fp in tree_files:
                parts = fp.split("/")
                if len(parts) >= 1:
                    dirs.add(parts[0])
                if len(parts) >= 2:
                    dirs.add(f"  {parts[0]}/{parts[1]}")

            structure = f"Repository: {event.repo_full_name}\n" + "\n".join(sorted(dirs)[:50])
            return budget.allocate(
                "repo_structure", structure,
                max_tokens=settings.context_structure_max_tokens,
            )
        except Exception as e:
            logger.debug("Repo structure fetch failed: %s", e)
            return f"Repository: {event.repo_full_name}"
