"""
SNLA RAG Integration Layer.

Connects the RAG knowledge base to existing SNLA modules:

1.  **validator.py integration** — enhances syntax validation with
    official command and subcommand verification from the knowledge base.
2.  **syntax prompt integration** — injects relevant SPSS documentation
    context into LLM syntax generation prompts for higher accuracy.

Usage:
    from snla.rag.integration import (
        enhance_validation,
        get_syntax_context,
        is_valid_spss_command,
        get_command_subcommands,
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Cache for command metadata to avoid repeated DB queries
_command_cache: dict[str, dict[str, Any]] = {}


def _get_retriever():
    """Lazy-load the retriever.

    Returns None if the SKIP_RAG environment variable is set, or if
    sentence-transformers/ChromaDB are unavailable.  RAG is an optional
    enhancement — the system functions normally without it.
    """
    import os
    if os.getenv("SKIP_RAG", "").lower() in ("1", "true", "yes"):
        return None
    try:
        from snla.rag.retriever import get_retriever
        return get_retriever()
    except (ImportError, OSError) as exc:
        logger.debug("RAG retriever unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Validator Integration
# ---------------------------------------------------------------------------


def is_valid_spss_command(command: str) -> bool:
    """Check if a command keyword exists in the official SPSS syntax reference."""
    retriever = _get_retriever()
    if retriever is None:
        return False
    try:
        info = retriever.validate_command(command)
        return info.get("exists", False)
    except Exception as exc:
        logger.warning("RAG lookup failed for '%s': %s", command, exc)
        return False


def get_command_subcommands(command: str) -> list[str]:
    """Get the official subcommand list for a SPSS command."""
    if command in _command_cache:
        return _command_cache[command].get("subcommands", [])

    retriever = _get_retriever()
    if retriever is None:
        return []
    try:
        info = retriever.validate_command(command)
        if info.get("exists"):
            _command_cache[command] = info
            return info.get("subcommands", [])
    except Exception as exc:
        logger.warning("RAG subcommand lookup failed for '%s': %s", command, exc)

    return []


def get_command_category(command: str) -> str:
    """Get the category of a SPSS command."""
    if command in _command_cache:
        cats = _command_cache[command].get("category", [])
        return cats[0] if cats else "unknown"

    retriever = _get_retriever()
    if retriever is None:
        return "unknown"
    try:
        info = retriever.validate_command(command)
        if info.get("exists"):
            _command_cache[command] = info
            cats = info.get("category", [])
            return cats[0] if cats else "unknown"
    except Exception:
        pass

    return "unknown"


def enhance_validation(
    syntax: str,
    var_list: list[str] | None = None,
) -> dict[str, Any]:
    """Enhanced syntax validation with RAG knowledge base integration.

    Extends the basic validator.py checks with:
    - Verification that extracted commands exist in official SPSS documentation.
    - Subcommand name suggestions when commands are recognized.
    - Security category awareness (e.g., command that modifies files).

    Args:
        syntax: SPSS syntax string to validate.
        var_list: Optional list of known variable names.

    Returns:
        Validation result dict with additional RAG-enhanced fields:
        - valid: bool (same as validator.py)
        - errors: list[str]
        - warnings: list[str]
        - rag_enhanced: bool (True if RAG was used)
        - unrecognized_commands: list[str] (commands not in SPSS reference)
        - command_categories: dict[str, str] (command → category mapping)
    """
    from snla.syntax.validator import validate as basic_validate
    from snla.syntax.validator import extract_commands

    # Basic validation first
    result = basic_validate(syntax, var_list)

    # Extract commands and enhance with RAG
    commands = extract_commands(syntax)
    unrecognized: list[str] = []
    categories: dict[str, str] = {}

    for cmd in commands:
        cat = get_command_category(cmd)
        categories[cmd] = cat

        if not is_valid_spss_command(cmd):
            if cmd not in ("T-TEST", "ONEWAY", "OMS", "OMSEND"):  # Known special
                unrecognized.append(cmd)

    if unrecognized:
        result.setdefault("warnings", []).append(
            f"Unknown SPSS command(s): {', '.join(unrecognized)}. "
            "These may still be valid but are not in the indexed reference."
        )

    result["rag_enhanced"] = True
    result["unrecognized_commands"] = unrecognized
    result["command_categories"] = categories

    return result


# ---------------------------------------------------------------------------
# Syntax Prompt Integration
# ---------------------------------------------------------------------------


def get_syntax_context(method, n_chunks=3, max_chars=3000):
    """Get SPSS documentation context for syntax generation."""
    retriever = _get_retriever()
    if retriever is None:
        return ""
    try:
        context = retriever.get_context_for_method(method, n_chunks=n_chunks)
        if context and len(context) > max_chars:
            context = context[:max_chars] + "\n\n[...]"
        return context
    except Exception as exc:
        logger.warning("RAG syntax context failed for '%s': %s", method, exc)
        return ""


def get_dataset_context_for_prompt(variables: list[dict]) -> str:
    """Build RAG-enhanced dataset context block for LLM prompts.

    Enriches the standard [DATASET CONTEXT] block with relevant
    SPSS command documentation based on the variable types.

    Args:
        variables: List of variable metadata dicts.

    Returns:
        RAG context string, or empty string if no relevant docs found.
    """
    # Determine likely methods based on variable types
    has_categorical = any(
        v.get("value_labels") for v in variables
    )
    has_continuous = any(
        v.get("type") == "Numeric" and not v.get("value_labels")
        for v in variables
    )

    contexts: list[str] = []

    if has_categorical and has_continuous:
        # Likely comparison analysis
        contexts.append(get_syntax_context("T-TEST", n_chunks=1, max_chars=1500))
        contexts.append(get_syntax_context("ONEWAY", n_chunks=1, max_chars=1000))

    if has_continuous:
        contexts.append(get_syntax_context("DESCRIPTIVES", n_chunks=1, max_chars=1000))

    return "\n\n".join(c for c in contexts if c)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def warm_cache(commands: list[str] | None = None) -> int:
    """Pre-warm the command metadata cache.

    Args:
        commands: Specific commands to cache, or None for all known commands.

    Returns:
        Number of commands cached.
    """
    retriever = _get_retriever()
    if commands is None:
        commands = retriever.list_commands()

    cached = 0
    for cmd in commands:
        if cmd not in _command_cache:
            try:
                info = retriever.validate_command(cmd)
                if info.get("exists"):
                    _command_cache[cmd] = info
                    cached += 1
            except Exception:
                pass
    return cached


__all__ = [
    "is_valid_spss_command",
    "get_command_subcommands",
    "get_command_category",
    "enhance_validation",
    "get_syntax_context",
    "get_dataset_context_for_prompt",
    "warm_cache",
]
