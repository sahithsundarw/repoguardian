"""
ChromaDB vector store service.

Stores code chunks as embeddings for semantic similarity search.
Used by the Context Retrieval Agent to find related code across the
entire repository that is semantically similar to the PR diff.

Each code chunk is stored with metadata:
  - repo_id, file_path, start_line, end_line, language
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from backend.config import get_settings
from backend.models.schemas import SimilarChunk

logger = logging.getLogger(__name__)
settings = get_settings()

_chroma_client = None
_collection = None


def _get_client():
    global _chroma_client
    if _chroma_client is None:
        try:
            import chromadb
            _chroma_client = chromadb.HttpClient(
                host=settings.chroma_host,
                port=settings.chroma_port,
            )
        except Exception as e:
            logger.warning("ChromaDB not available: %s — using in-memory fallback", e)
            import chromadb
            _chroma_client = chromadb.Client()  # in-memory fallback
    return _chroma_client


def _get_collection():
    global _collection
    if _collection is None:
        client = _get_client()
        _collection = client.get_or_create_collection(
            name=settings.chroma_collection_code,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


# ── Public API ─────────────────────────────────────────────────────────────────


def upsert_code_chunks(
    repo_id: str,
    file_path: str,
    chunks: list[dict[str, Any]],
) -> int:
    """
    Upsert code chunks into the vector store.

    Args:
        repo_id:   Repository UUID string.
        file_path: Source file path.
        chunks:    List of dicts with keys: source, start_line, end_line, language.

    Returns:
        Number of chunks upserted.
    """
    if not chunks:
        return 0

    collection = _get_collection()
    ids, documents, metadatas = [], [], []

    for chunk in chunks:
        chunk_id = _make_chunk_id(repo_id, file_path, chunk["start_line"])
        ids.append(chunk_id)
        documents.append(chunk["source"])
        metadatas.append({
            "repo_id": repo_id,
            "file_path": file_path,
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "language": chunk.get("language", "unknown"),
        })

    try:
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logger.debug("Upserted %d chunks from %s", len(chunks), file_path)
        return len(chunks)
    except Exception as e:
        logger.error("Failed to upsert code chunks: %s", e)
        return 0


def search_similar(
    query_text: str,
    repo_id: str,
    top_k: int = 5,
    min_similarity: float = 0.6,
) -> list[SimilarChunk]:
    """
    Find the top-k most semantically similar code chunks to `query_text`.

    Args:
        query_text:     The text to search for (usually the PR diff summary).
        repo_id:        Limit search to this repository.
        top_k:          Maximum number of results.
        min_similarity: Minimum cosine similarity (0–1). Lower bound filter.

    Returns:
        List of SimilarChunk objects, sorted by similarity descending.
    """
    collection = _get_collection()

    try:
        results = collection.query(
            query_texts=[query_text],
            n_results=top_k * 2,  # fetch extra to allow filtering
            where={"repo_id": repo_id},
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error("ChromaDB query failed: %s", e)
        return []

    chunks: list[SimilarChunk] = []
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for doc, meta, dist in zip(documents, metadatas, distances):
        # Chroma returns cosine distance (0=identical, 2=opposite)
        # Convert to similarity score in [0, 1]
        similarity = max(0.0, 1.0 - dist)
        if similarity < min_similarity:
            continue
        chunks.append(SimilarChunk(
            file_path=meta["file_path"],
            start_line=int(meta["start_line"]),
            end_line=int(meta["end_line"]),
            source=doc,
            similarity_score=round(similarity, 4),
        ))
        if len(chunks) >= top_k:
            break

    return sorted(chunks, key=lambda c: c.similarity_score, reverse=True)


def delete_repo_chunks(repo_id: str) -> int:
    """Remove all indexed chunks for a repository (e.g. on repo deregistration)."""
    collection = _get_collection()
    try:
        existing = collection.get(where={"repo_id": repo_id})
        ids = existing.get("ids", [])
        if ids:
            collection.delete(ids=ids)
        return len(ids)
    except Exception as e:
        logger.error("Failed to delete chunks for repo %s: %s", repo_id, e)
        return 0


def index_repository_files(
    repo_id: str,
    file_contents: list[dict[str, Any]],
) -> int:
    """
    Index all files in a repository by splitting them into function/class
    chunks and upserting them into the vector store.

    Args:
        repo_id:       Repository UUID.
        file_contents: List of dicts: {path, content, language}.

    Returns:
        Total chunks indexed.
    """
    from backend.utils.ast_extractor import extract_all_symbols, detect_language

    total = 0
    for fc in file_contents:
        path = fc["path"]
        content = fc["content"]
        language = detect_language(path)

        if language == "unknown":
            continue  # Skip unsupported files

        symbols = extract_all_symbols(content, path)
        chunks = [
            {
                "source": sym.full_source,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "language": language,
            }
            for sym in symbols
            if len(sym.full_source.strip()) > 20  # Skip trivial symbols
        ]

        if chunks:
            total += upsert_code_chunks(repo_id, path, chunks)

    return total


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_chunk_id(repo_id: str, file_path: str, start_line: int) -> str:
    """Create a stable, unique ID for a code chunk."""
    raw = f"{repo_id}:{file_path}:{start_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
