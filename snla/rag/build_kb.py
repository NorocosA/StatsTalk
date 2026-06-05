"""
One-shot RAG knowledge base builder.

Usage:
    python -m snla.rag.build_kb                     # Build with all essential commands
    python -m snla.rag.build_kb --all               # Build with ALL commands in PDF
    python -m snla.rag.build_kb --cmds FREQUENCIES,T-TEST,REGRESSION  # Specific commands
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("snla.rag.build_kb")


def build(
    pdf_path: str,
    commands: list[str] | None = None,
    persist_dir: str | None = None,
) -> dict:
    """Build the RAG knowledge base from the SPSS command syntax PDF.

    Args:
        pdf_path: Path to the SPSS Command Syntax Reference PDF.
        commands: Specific commands to index (None = essential only).
        persist_dir: ChromaDB persistence directory.

    Returns:
        Statistics dict with timing and counts.
    """
    from snla.rag.chunker import CommandChunk, extract_command_chunks
    from snla.rag.embedder import embed_texts
    from snla.rag.store import add_chunks, collection_stats

    t0 = time.perf_counter()

    # ---- Phase 1: Chunk ----
    logger.info("Phase 1: Chunking PDF...")
    if commands:
        logger.info("  Target commands: %s", commands)
    else:
        logger.info("  Target: all SNLA essential commands")

    chunk_objects: list[CommandChunk] = extract_command_chunks(pdf_path, commands=commands)
    chunks = [c.to_dict() for c in chunk_objects]
    chunk_time = time.perf_counter() - t0
    logger.info(
        "  Extracted %d chunks from %d commands in %.1fs",
        len(chunks),
        len({c.command for c in chunk_objects}),
        chunk_time,
    )

    if not chunks:
        logger.error("No chunks extracted! Check PDF path and TOC extraction.")
        return {"success": False, "error": "No chunks extracted"}

    # ---- Phase 2: Embed ----
    t1 = time.perf_counter()
    logger.info("Phase 2: Embedding %d chunks...", len(chunks))
    texts = [c["content"] for c in chunks]
    embeddings = embed_texts(texts, show_progress=True)
    embed_time = time.perf_counter() - t1
    logger.info("  Embedded in %.1fs (%.1f chunks/s)", embed_time, len(chunks) / embed_time)

    # ---- Phase 3: Store ----
    t2 = time.perf_counter()
    logger.info("Phase 3: Storing in ChromaDB...")
    added = add_chunks(chunks, embeddings, persist_dir=persist_dir)
    store_time = time.perf_counter() - t2
    logger.info("  Stored %d chunks in %.1fs", added, store_time)

    # ---- Stats ----
    stats = collection_stats(persist_dir)
    total_time = time.perf_counter() - t0

    logger.info("=" * 50)
    logger.info("Build complete in %.1fs", total_time)
    logger.info("  Chunks:    %d", stats["total_chunks"])
    logger.info("  Commands:  %d", stats["unique_commands"])
    for cat, count in stats.get("categories", {}).items():
        logger.info("    %s: %d", cat, count)

    return {
        "success": True,
        "chunks": len(chunks),
        "total_time_s": total_time,
        "chunk_time_s": chunk_time,
        "embed_time_s": embed_time,
        "store_time_s": store_time,
        "stats": stats,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build SNLA RAG knowledge base from SPSS Command Syntax PDF"
    )
    parser.add_argument(
        "--pdf",
        default=None,
        help="Path to SPSS Command Syntax Reference PDF",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Index ALL commands (default: essential only)",
    )
    parser.add_argument(
        "--cmds",
        default=None,
        help="Comma-separated list of specific commands to index",
    )
    parser.add_argument(
        "--persist-dir",
        default=None,
        help="ChromaDB persistence directory",
    )

    args = parser.parse_args()

    # Determine PDF path
    pdf_path = args.pdf
    if not pdf_path:
        # Try known locations
        PROJECT_ROOT = Path(__file__).resolve().parent.parent
        candidates = [
            str(PROJECT_ROOT / "IBM_SPSS26_Instruction" / "IBM_SPSS_Statistics_Command_Syntax_Reference.pdf"),
            str(
                Path(__file__).resolve().parent.parent.parent
                / "IBM_SPSS26_Instruction"
                / "IBM_SPSS_Statistics_Command_Syntax_Reference.pdf"
            ),
        ]
        import os

        for cand in candidates:
            if os.path.exists(cand):
                pdf_path = cand
                break

    if not pdf_path or not os.path.exists(pdf_path):
        logger.error("PDF not found. Use --pdf to specify the path.")
        sys.exit(1)

    # Determine commands to extract
    commands: list[str] | None = None
    if args.cmds:
        commands = [c.strip().upper() for c in args.cmds.split(",")]
    elif not args.all:
        from snla.rag.chunker import SNLA_ESSENTIAL_COMMANDS

        commands = list(SNLA_ESSENTIAL_COMMANDS)

    result = build(pdf_path, commands=commands, persist_dir=args.persist_dir)
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
