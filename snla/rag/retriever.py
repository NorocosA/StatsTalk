"""
SNLA RAG Retriever — High-level retrieval interface for the SPSS knowledge base.

Provides two primary retrieval modes:
1. **Semantic search** — find relevant syntax documentation by NL query.
2. **Command lookup** — retrieve exact command documentation.

Integration points:
    - validator.py: verify command whitelist against knowledge base
    - syntax prompt builder: inject relevant few-shot examples
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Retriever:
    """High-level retrieval interface for the SPSS syntax knowledge base.

    Usage::

        retriever = Retriever()
        results = retriever.search("independent t-test with grouping variable")
        cmd_docs = retriever.get_command("FREQUENCIES")
    """

    def __init__(self, persist_dir: str | None = None) -> None:
        self._persist_dir = persist_dir

    # ---- Semantic Search ----

    def search(
        self,
        query: str,
        n_results: int = 5,
        filter_category: str | None = None,
        hybrid: bool = True,
    ) -> list[dict[str, Any]]:
        """Hybrid search over the SPSS knowledge base.

        Combines semantic embedding search with keyword-based command name
        boosting.  When *hybrid* is True and the query contains or suggests
        a known SPSS command, exact command matches receive a strong score
        boost, demoting unrelated results.

        Args:
            query: Natural language query.
            n_results: Number of results.
            filter_category: Optional category filter.
            hybrid: Enable hybrid keyword+semantic boosting (default True).

        Returns:
            List of result dicts with document, metadata, distance.
        """
        from snla.rag.embedder import embed_single
        from snla.rag.store import search as _search

        embedding = embed_single(query)
        results = _search(
            query=query,
            query_embedding=embedding,
            n_results=n_results * 2 if hybrid else n_results,
            filter_category=filter_category,
            persist_dir=self._persist_dir,
        )

        if not hybrid:
            return results[:n_results]

        # ---- Hybrid boost: detect SPSS command names in query ----
        boosted = self._apply_command_boost(query, results)
        return boosted[:n_results]

    def _apply_command_boost(
        self,
        query: str,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Boost results whose command name appears in or relates to the query.

        Scans *query* for known SPSS command names and subcommand keywords.
        Results whose metadata command matches a detected name get a
        substantial distance reduction (boost).

        Args:
            query: The user's natural language query.
            results: Semantic search results to re-rank.

        Returns:
            Re-ranked results with adjusted distances.
        """
        known_commands: set[str] = set(self.list_commands())
        query_upper = query.upper()

        # Find command names explicitly mentioned in the query
        matched_commands: set[str] = set()
        for cmd in known_commands:
            if cmd in query_upper:
                matched_commands.add(cmd)

        # Also match common NL → command mappings
        nl_to_cmd: dict[str, str] = {
            "T-TEST": "T-TEST",
            "T TEST": "T-TEST",
            "ANOVA": "ONEWAY",
            "CHI SQUARE": "CROSSTABS",
            "CHI-SQUARE": "CROSSTABS",
            "CROSS TAB": "CROSSTABS",
            "CROSSTAB": "CROSSTABS",
            "FREQUENCY": "FREQUENCIES",
            "DESCRIPTIVE": "DESCRIPTIVES",
            "CORRELATION": "CORRELATIONS",
            "REGRESSION": "REGRESSION",
            "COMPUTE": "COMPUTE",
            "RECODE": "RECODE",
            "FILTER": "FILTER",
            "WEIGHT": "WEIGHT",
        }
        for nl_term, cmd_name in nl_to_cmd.items():
            if nl_term in query_upper:
                matched_commands.add(cmd_name)

        if not matched_commands:
            return results  # No boost applicable

        # Apply boost: reduce distance for matching command results
        BOOST_FACTOR = 0.3  # Multiplier on distance (lower = better)
        boosted = []
        for r in results:
            meta_cmd = r.get("metadata", {}).get("command", "")
            if meta_cmd in matched_commands:
                # Significant boost for exact command matches
                r_copy = dict(r)
                r_copy["distance"] = r["distance"] * BOOST_FACTOR
                r_copy["_boosted"] = True
                boosted.append(r_copy)
            else:
                boosted.append(r)

        # Re-sort by distance (lower is better in ChromaDB)
        boosted.sort(key=lambda x: x["distance"])
        return boosted

    # ---- Exact Command Lookup ----

    def get_command(self, command: str) -> list[dict[str, Any]]:
        """Retrieve full documentation for a specific SPSS command.

        Args:
            command: Exact command name (e.g., 'FREQUENCIES', 'T-TEST').

        Returns:
            List of chunk dicts, sorted by page.
        """
        from snla.rag.store import get_by_command

        return get_by_command(command, persist_dir=self._persist_dir)

    # ---- Knowledge Base Info ----

    def list_commands(self) -> list[str]:
        """List all indexed command names."""
        from snla.rag.store import list_commands

        return list_commands(self._persist_dir)

    def stats(self) -> dict[str, Any]:
        """Get knowledge base statistics."""
        from snla.rag.store import collection_stats

        return collection_stats(self._persist_dir)

    # ---- Validator Integration ----

    def validate_command(self, command: str) -> dict[str, Any]:
        """Check if a command exists in the knowledge base and return its info.

        Used by validator.py to enhance validation with official subcommand lists.

        Args:
            command: SPSS command name to validate.

        Returns:
            Dict with 'exists', 'category', 'subcommands', 'page'.
        """
        docs = self.get_command(command)
        if not docs:
            return {"exists": False, "command": command}

        # Aggregate metadata from all chunks
        categories: set[str] = set()
        all_subcommands: set[str] = set()
        page_start: int | None = None

        for doc in docs:
            meta = doc.get("metadata", {})
            cat = meta.get("category", "")
            if cat:
                categories.add(cat)
            subs = meta.get("subcommands", "")
            if subs:
                for s in subs.split(","):
                    s = s.strip()
                    if s:
                        all_subcommands.add(s)
            ps = meta.get("page_start", 0)
            if page_start is None or (ps > 0 and ps < page_start):
                page_start = ps

        return {
            "exists": True,
            "command": command,
            "category": list(categories),
            "subcommands": sorted(all_subcommands),
            "page": page_start or 0,
            "chunks": len(docs),
        }

    # ---- Syntax Prompt Integration ----

    def get_context_for_method(self, method: str, n_chunks: int = 3) -> str:
        """Retrieve relevant documentation context for a statistical method.

        Used to augment LLM syntax generation prompts with official
        SPSS command documentation.

        Args:
            method: Method name (e.g., 'independent_t_test', 'oneway_anova').
            n_chunks: Number of relevant chunks to include.

        Returns:
            Concatenated context string for prompt injection.
        """
        # Map SNLA method names to SPSS commands
        method_to_command: dict[str, str] = {
            "independent_t_test": "T-TEST",
            "paired_t_test": "T-TEST",
            "oneway_anova": "ONEWAY",
            "simple_regression": "REGRESSION",
            "pearson_correlation": "CORRELATIONS",
            "chi_square": "CROSSTABS",
            "frequencies": "FREQUENCIES",
            "descriptives": "DESCRIPTIVES",
        }

        command = method_to_command.get(method)
        if command is None:
            # Method may already be an SPSS command name (e.g., 'T-TEST')
            command = method
            docs = self.get_command(command)
            if not docs:
                docs = self.search(method, n_results=n_chunks)
        else:
            docs = self.get_command(command)
            if not docs:
                docs = self.search(method, n_results=n_chunks)

        if not docs:
            return ""

        # Compile context
        parts: list[str] = [
            f"[SPSS OFFICIAL DOCUMENTATION: {command or method}]",
        ]
        for doc in docs[:n_chunks]:
            content = doc.get("document", "")
            if content:
                parts.append(content[:2000])  # Truncate per chunk

        return "\n\n---\n\n".join(parts)


# ---- Module-level convenience ----

_default_retriever: Retriever | None = None


def get_retriever(persist_dir: str | None = None) -> Retriever:
    """Get or create the default retriever instance."""
    global _default_retriever
    if _default_retriever is None:
        _default_retriever = Retriever(persist_dir)
    return _default_retriever


__all__ = ["Retriever", "get_retriever"]
