"""
ChromaDB Vector Store for SNLA RAG.

Manages the persistent vector store for SPSS command syntax documentation.
Supports adding, searching, and filtering chunks by metadata.

Collection: "spss_syntax_reference"
Metadata: {command, chunk_id, chunk_type, page_start, page_end, subcommands, category}
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default ChromaDB persistence directory
DEFAULT_PERSIST_DIR: str = str(Path(__file__).resolve().parent.parent.parent / ".chromadb")

COLLECTION_NAME: str = "spss_syntax_reference"

_client: Any = None
_collection: Any = None


def _get_client(persist_dir: str | None = None) -> Any:
    """Get or create a persistent ChromaDB client."""
    global _client
    if _client is not None:
        return _client

    import chromadb

    directory = persist_dir or DEFAULT_PERSIST_DIR
    os.makedirs(directory, exist_ok=True)
    _client = chromadb.PersistentClient(path=directory)
    logger.info("ChromaDB client initialised at %s", directory)
    return _client


def get_collection(persist_dir: str | None = None) -> Any:
    """Get or create the SNLA RAG collection."""
    global _collection
    if _collection is not None:
        return _collection

    client = _get_client(persist_dir)

    # Try to get existing collection, create if not found
    try:
        _collection = client.get_collection(COLLECTION_NAME)
        logger.info(
            "Using existing collection '%s' (%d documents)",
            COLLECTION_NAME,
            _collection.count(),
        )
    except Exception:
        _collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"description": "IBM SPSS Command Syntax Reference chunks"},
        )
        logger.info("Created new collection '%s'", COLLECTION_NAME)

    return _collection


def add_chunks(
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    persist_dir: str | None = None,
    batch_size: int = 100,
) -> int:
    """Add document chunks with embeddings to the vector store.

    Args:
        chunks: List of chunk dicts from chunker (must have 'content', 'chunk_id',
                'command', 'chunk_type', 'page_start', 'category', etc.).
        embeddings: Corresponding embedding vectors.
        persist_dir: ChromaDB persistence directory.
        batch_size: Number of items per batch insert.

    Returns:
        Number of chunks added.
    """
    collection = get_collection(persist_dir)
    total = len(chunks)

    for i in range(0, total, batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_embeddings = embeddings[i : i + batch_size]

        ids = [c["chunk_id"] for c in batch_chunks]
        documents = [c["content"] for c in batch_chunks]
        metadatas = [
            {
                "command": c.get("command", ""),
                "chunk_type": c.get("chunk_type", ""),
                "page_start": c.get("page_start", 0),
                "page_end": c.get("page_end", 0),
                "category": c.get("category", ""),
                "subcommands": ",".join(c.get("subcommands", [])),
                "keywords": ",".join(c.get("keywords", [])[:20]),
            }
            for c in batch_chunks
        ]

        collection.add(
            ids=ids,
            documents=documents,
            embeddings=batch_embeddings,
            metadatas=metadatas,
        )

    logger.info("Added %d chunks to collection '%s'", total, COLLECTION_NAME)
    return total


def search(
    query: str,
    query_embedding: list[float],
    n_results: int = 5,
    filter_cmd: str | None = None,
    filter_category: str | None = None,
    persist_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search over the SPSS syntax knowledge base.

    Args:
        query: Natural language query string.
        query_embedding: Pre-computed embedding for the query.
        n_results: Number of top results to return.
        filter_cmd: Optional exact command name filter.
        filter_category: Optional category filter (e.g., 'descriptive', 'comparison').
        persist_dir: ChromaDB persistence directory.

    Returns:
        List of result dicts with keys: id, document, metadata, distance.
    """
    collection = get_collection(persist_dir)

    # Build metadata filter
    where: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] = []
    if filter_cmd:
        conditions.append({"command": filter_cmd})
    if filter_category:
        conditions.append({"category": filter_category})

    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    # Normalise ChromaDB response format
    formatted: list[dict[str, Any]] = []
    if results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            formatted.append(
                {
                    "id": doc_id,
                    "document": (results["documents"][0][i] if results["documents"] else ""),
                    "metadata": (results["metadatas"][0][i] if results["metadatas"] else {}),
                    "distance": (results["distances"][0][i] if results["distances"] else 0.0),
                }
            )

    return formatted


def get_by_command(
    command: str,
    persist_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve all chunks for a specific SPSS command (exact lookup).

    Args:
        command: Exact command name (e.g., 'FREQUENCIES', 'T-TEST').
        persist_dir: ChromaDB persistence directory.

    Returns:
        List of result dicts sorted by page.
    """
    collection = get_collection(persist_dir)
    results = collection.get(
        where={"command": command},
        include=["documents", "metadatas"],
    )

    formatted: list[dict[str, Any]] = []
    if results["ids"]:
        for i, doc_id in enumerate(results["ids"]):
            formatted.append(
                {
                    "id": doc_id,
                    "document": results["documents"][i] if results["documents"] else "",
                    "metadata": results["metadatas"][i] if results["metadatas"] else {},
                }
            )

    # Sort by page number
    formatted.sort(key=lambda x: x["metadata"].get("page_start", 0))
    return formatted


def list_commands(persist_dir: str | None = None) -> list[str]:
    """List all unique command names in the knowledge base.

    Args:
        persist_dir: ChromaDB persistence directory.

    Returns:
        Sorted list of command names.
    """
    collection = get_collection(persist_dir)
    all_meta = collection.get(include=["metadatas"])
    commands: set[str] = set()
    if all_meta["metadatas"]:
        for meta in all_meta["metadatas"]:
            if "command" in meta:
                commands.add(meta["command"])
    return sorted(commands)


def collection_stats(persist_dir: str | None = None) -> dict[str, Any]:
    """Get collection statistics.

    Args:
        persist_dir: ChromaDB persistence directory.

    Returns:
        Dict with count, command_count, category_counts.
    """
    collection = get_collection(persist_dir)
    count = collection.count()
    all_meta = collection.get(include=["metadatas"])

    cmd_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}
    if all_meta["metadatas"]:
        for meta in all_meta["metadatas"]:
            cmd = meta.get("command", "unknown")
            cat = meta.get("category", "other")
            cmd_counts[cmd] = cmd_counts.get(cmd, 0) + 1
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    return {
        "total_chunks": count,
        "unique_commands": len(cmd_counts),
        "commands": dict(sorted(cmd_counts.items())),
        "categories": dict(sorted(cat_counts.items())),
    }


__all__ = [
    "get_collection",
    "add_chunks",
    "search",
    "get_by_command",
    "list_commands",
    "collection_stats",
    "COLLECTION_NAME",
    "DEFAULT_PERSIST_DIR",
]
