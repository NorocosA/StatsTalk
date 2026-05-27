"""
SPSS Command Syntax Reference PDF Chunker.

Parses the 2500+ page IBM SPSS Command Syntax Reference PDF into
command-level chunks suitable for embedding and retrieval.

Strategy:
    1. Extract TOC from front matter (pages 3-35) to get command→page mapping.
    2. Scan body pages to detect command section boundaries (title pages).
    3. Extract content for each command: Overview + Subcommands + Examples.
    4. Split into manageable chunks with rich metadata.

Output: list[dict] — each dict has keys:
    - command: str          (e.g., "FREQUENCIES", "T-TEST")
    - chunk_id: str         (e.g., "FREQUENCIES_0", "FREQUENCIES_1")
    - chunk_type: str       ("overview", "subcommand", "example", "syntax")
    - content: str          (the text content)
    - page_start: int
    - page_end: int
    - subcommands: list[str]
    - category: str         (e.g., "descriptive", "comparison", "regression")
    - keywords: list[str]   (extracted SPSS keywords)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Commands essential for SNLA P0 operations
SNLA_ESSENTIAL_COMMANDS: set[str] = {
    "FREQUENCIES",
    "DESCRIPTIVES",
    "T-TEST",
    "CROSSTABS",
    "REGRESSION",
    "ONEWAY",
    "OMS",
    "OMSEND",
    "OMSLINFO",
    "AUTORECODE",
    "RECODE",
    "COMPUTE",
    "SELECT IF",
    "FILTER",
    "SORT CASES",
    "WEIGHT",
    "RENAME VARIABLES",
    "CORRELATIONS",
    "NONPAR CORR",
    "NPAR TESTS",
    "EXAMINE",
}

# Category mapping for common SPSS commands
COMMAND_CATEGORIES: dict[str, str] = {
    "FREQUENCIES": "descriptive",
    "DESCRIPTIVES": "descriptive",
    "EXAMINE": "descriptive",
    "T-TEST": "comparison",
    "ONEWAY": "comparison",
    "ANOVA": "comparison",
    "CROSSTABS": "crosstabulation",
    "CORRELATIONS": "correlation",
    "NONPAR CORR": "correlation",
    "REGRESSION": "regression",
    "LOGISTIC REGRESSION": "regression",
    "NPAR TESTS": "nonparametric",
    "OMS": "output_management",
    "OMSEND": "output_management",
    "COMPUTE": "transformation",
    "RECODE": "transformation",
    "AUTORECODE": "transformation",
    "SELECT IF": "transformation",
    "FILTER": "transformation",
    "SORT CASES": "transformation",
    "RENAME VARIABLES": "transformation",
    "WEIGHT": "transformation",
    "AGGREGATE": "transformation",
    "ADD FILES": "data_management",
    "MATCH FILES": "data_management",
    "GET FILE": "data_management",
    "SAVE": "data_management",
    "DATASET": "data_management",
}

# Keywords indicating subcommand sections
SUBCOMMAND_MARKERS: list[str] = [
    "Subcommand",
    "subcommand",
    "Keyword",
    "keyword",
]

# TOC front matter page range (0-indexed)
TOC_START_PAGE: int = 2  # Page 3
TOC_END_PAGE: int = 75  # Page 76 (TOC extends ~65 pages in this version)

# The TOC in this PDF edition was generated from a different printing.
# All TOC page numbers are systematically 70 pages behind the actual
# PDF physical page indices.  Verified: FINISH (TOC 703 → PDF 773),
# FREQUENCIES (TOC 719 → PDF 789), T-TEST (TOC 2189 → PDF 2259).
TOC_PAGE_OFFSET: int = 70


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CommandChunk:
    """A single chunk of SPSS command documentation."""

    command: str
    chunk_id: str
    chunk_type: str  # "overview", "subcommand", "example", "full"
    content: str
    page_start: int
    page_end: int
    subcommands: list[str] = field(default_factory=list)
    category: str = ""
    keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "chunk_id": self.chunk_id,
            "chunk_type": self.chunk_type,
            "content": self.content,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "subcommands": self.subcommands,
            "category": self.category,
            "keywords": self.keywords,
        }


# ---------------------------------------------------------------------------
# TOC Parser
# ---------------------------------------------------------------------------


def extract_toc(pdf) -> list[tuple[str, int]]:
    """Extract command→page mapping from the TOC front matter.

    Args:
        pdf: An open pdfplumber.PDF instance.

    Returns:
        List of (command_name, start_page) tuples sorted by page.
    """
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()

    for i in range(TOC_START_PAGE, min(TOC_END_PAGE, len(pdf.pages))):
        text = pdf.pages[i].extract_text()
        if not text:
            continue
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Match: COMMAND_NAME (possibly multi-word) ....... page_number
            # The PDF preserves dot-leaders: "FREQUENCIES........................................................................719"
            # Pattern: name starts with uppercase, followed by word chars/spaces,
            # then 3+ dots (leader), then page number at end of line.
            m = re.match(
                r"^([A-Z][A-Za-z0-9\s\-\/\(\)]+?)\.{3,}\s*(\d{1,4})\s*$",
                line,
            )
            if m:
                name = m.group(1).strip()
                page = int(m.group(2))
                # Filter: must be a real command name (all caps start, >2 chars)
                if (
                    len(name) > 2
                    and name[0].isupper()
                    and name not in seen
                    and page < 2400
                    and not name.startswith("Contents")
                    and not name.startswith("Introduction")
                    and not name.startswith("Chapter")
                    and not name.startswith("Appendix")
                    and "Copyright" not in name
                ):
                    entries.append((name, page))
                    seen.add(name)

    # Sort by page number
    entries.sort(key=lambda x: x[1])
    return entries


def _get_command_category(command: str) -> str:
    """Determine the category of a command."""
    return COMMAND_CATEGORIES.get(command.upper(), "other")


# ---------------------------------------------------------------------------
# Content Extractor
# ---------------------------------------------------------------------------


def _page_to_index(page_num: int) -> int:
    """Convert a 1-based page number to 0-based PDF index."""
    return page_num - 1


def _extract_subcommands(text: str) -> list[str]:
    """Extract subcommand names from command documentation text."""
    subcommands: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^([A-Z][A-Z\s]+)\s+Subcommand", line)
        if m:
            subcommands.append(m.group(1).strip())
    return list(dict.fromkeys(subcommands))  # deduplicate, preserve order


def _extract_keywords(text: str) -> list[str]:
    """Extract SPSS keywords (all-caps identifiers) from text."""
    keywords: set[str] = set()
    for match in re.finditer(r"\b([A-Z]{2,}(?:\s+[A-Z]{2,})*)\b", text):
        kw = match.group(1)
        if 3 <= len(kw) <= 40 and not kw.startswith("IBM"):
            keywords.add(kw)
    return sorted(keywords, key=len, reverse=True)[:30]


# ---------------------------------------------------------------------------
# Content Boundary Detection (replaces TOC page number reliance)
# ---------------------------------------------------------------------------


def _detect_command_start(
    pdf,
    toc_page: int,
    command_name: str,
    search_window: int = 30,
) -> int:
    """Find the actual PDF page index where a command section starts.

    Scans forward from *toc_page - 5* within *search_window* pages, looking for
    the command's section header — the command name appearing as a standalone
    upper-case line, promptly followed by "Overview".

    Args:
        pdf: An open pdfplumber.PDF instance.
        toc_page: The page number from the TOC (approximate, 1-based).
        command_name: The command name to locate (e.g., "FREQUENCIES").
        search_window: Maximum pages to scan forward.

    Returns:
        0-based PDF page index where the command section starts, or
        *toc_page* - 1 if not found (fallback to TOC position).
    """
    cmd_upper = command_name.strip().upper()
    start_scan = max(0, toc_page - 6)  # Scan from 5 pages before TOC claim
    end_scan = min(toc_page + search_window, len(pdf.pages))

    for p_idx in range(start_scan, end_scan):
        text = pdf.pages[p_idx].extract_text()
        if not text:
            continue
        lines = text.strip().split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.upper() == cmd_upper:
                # Verify: "Overview" should appear within the next 3 lines
                # (command title pages have Overview shortly after)
                for j in range(i + 1, min(i + 5, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.lower().startswith("overview"):
                        return p_idx
                # Also check if next page starts with Overview
                if p_idx + 1 < len(pdf.pages):
                    next_text = pdf.pages[p_idx + 1].extract_text()
                    if next_text:
                        next_lines = next_text.strip().split("\n")
                        if next_lines and next_lines[0].strip().lower().startswith("overview"):
                            return p_idx

    # Fallback: return TOC page (may be inaccurate)
    return toc_page - 1


def _clean_page_content(text: str) -> str:
    """Clean extracted PDF page content.

    Removes:
    - Page number artifacts (isolated numbers on their own line)
    - Header/footer boilerplate (roman numerals, "Chapter N")
    - Cross-reference markers like "(see page X)"
    - Excessive whitespace
    """
    lines = text.split("\n")
    cleaned: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            cleaned.append("")
            continue

        # Skip roman numeral headers/footers (page markers)
        if re.match(r"^[ivxlcdm]+$", stripped, re.IGNORECASE):
            continue

        # Skip standalone page numbers
        if re.match(r"^\d{1,4}$", stripped):
            continue

        # Skip "Chapter X" headers
        if re.match(r"^Chapter\s+\d+", stripped, re.IGNORECASE):
            continue

        cleaned.append(stripped)

    # Collapse multiple blank lines
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _is_command_title_page(text: str) -> bool:
    """Check if a page looks like a command section title page.

    Command title pages typically have a large, centered command name
    followed by "Overview" on the next page.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return False
    first_line = lines[0].upper()
    # Check: first line is ALL CAPS, 3-40 chars, and appears to be a command name
    return bool(re.match(r"^[A-Z][A-Z\s\-/()]{2,39}$", first_line))


# ---------------------------------------------------------------------------
# Refactored Chunk Extractor
# ---------------------------------------------------------------------------


def extract_command_chunks(
    pdf_path: str,
    commands: list[str] | None = None,
    max_chunk_chars: int = 2000,
) -> list[CommandChunk]:
    """Extract command documentation as chunked text blocks.

    Uses content-based boundary detection to find accurate command section
    start pages, then extracts and cleans the content.

    Args:
        pdf_path: Path to the SPSS Command Syntax Reference PDF.
        commands: Specific commands to extract (None = all commands from TOC).
        max_chunk_chars: Maximum characters per chunk before splitting.

    Returns:
        List of CommandChunk objects.
    """
    import pdfplumber

    pdf = pdfplumber.open(pdf_path)

    # 1. Extract TOC for approximate page mapping
    toc = extract_toc(pdf)
    if not toc:
        pdf.close()
        raise RuntimeError("Failed to extract TOC from PDF")

    # 2. Determine which commands to extract
    target_commands: set[str]
    if commands:
        target_commands = {c.upper() for c in commands}
    else:
        target_commands = {c.upper() for c, _ in toc}

    # Build TOC entries for target commands (apply page offset)
    toc_map: dict[str, int] = {}
    for cmd_name, page in toc:
        if cmd_name.upper() in target_commands:
            toc_map[cmd_name.upper()] = page + TOC_PAGE_OFFSET

    # 3. For each target command, find actual boundaries and extract
    chunks: list[CommandChunk] = []
    max_pages_per_cmd = 30

    for cmd_upper, toc_page in sorted(toc_map.items(), key=lambda x: x[1]):
        cmd_name = cmd_upper  # preserve casing from TOC

        # Find actual start page via content scanning
        actual_start_idx = _detect_command_start(pdf, toc_page, cmd_name)

        # Extract content from actual start, up to max_pages_per_cmd
        pages_text: list[str] = []
        pages_read = 0
        actual_start_page = actual_start_idx + 1  # Convert back to 1-based

        for p_idx in range(
            actual_start_idx,
            min(actual_start_idx + max_pages_per_cmd, len(pdf.pages)),
        ):
            page_text = pdf.pages[p_idx].extract_text()
            if page_text:
                cleaned = _clean_page_content(page_text)
                if cleaned:
                    pages_text.append(cleaned)
                    pages_read += 1

        full_text = "\n\n".join(pages_text)

        # Skip empty commands
        if not full_text.strip():
            continue

        actual_end_page = actual_start_page + pages_read - 1

        # Extract metadata from the properly bounded content
        subcommands = _extract_subcommands(full_text)
        category = _get_command_category(cmd_name)
        keywords = _extract_keywords(full_text)

        # Split into chunks if too long
        if len(full_text) <= max_chunk_chars:
            chunks.append(
                CommandChunk(
                    command=cmd_name,
                    chunk_id=f"{cmd_name}_0",
                    chunk_type="full",
                    content=full_text,
                    page_start=actual_start_page,
                    page_end=actual_end_page,
                    subcommands=subcommands,
                    category=category,
                    keywords=keywords,
                )
            )
        else:
            paragraphs = full_text.split("\n\n")
            current_chunk = ""
            chunk_idx = 0

            for para in paragraphs:
                if len(current_chunk) + len(para) < max_chunk_chars:
                    current_chunk += ("\n\n" if current_chunk else "") + para
                else:
                    if current_chunk:
                        chunks.append(
                            CommandChunk(
                                command=cmd_name,
                                chunk_id=f"{cmd_name}_{chunk_idx}",
                                chunk_type="subcommand",
                                content=current_chunk,
                                page_start=actual_start_page,
                                page_end=actual_end_page,
                                subcommands=subcommands,
                                category=category,
                                keywords=keywords,
                            )
                        )
                        chunk_idx += 1
                    current_chunk = para

            if current_chunk:
                chunks.append(
                    CommandChunk(
                        command=cmd_name,
                        chunk_id=f"{cmd_name}_{chunk_idx}",
                        chunk_type="subcommand",
                        content=current_chunk,
                        page_start=actual_start_page,
                        page_end=actual_end_page,
                        subcommands=subcommands,
                        category=category,
                        keywords=keywords,
                    )
                )

    pdf.close()
    return chunks


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def chunk_essential(pdf_path: str) -> list[CommandChunk]:
    """Extract chunks for SNLA-essential commands only."""
    return extract_command_chunks(pdf_path, commands=list(SNLA_ESSENTIAL_COMMANDS))


def chunk_all(pdf_path: str) -> list[CommandChunk]:
    """Extract chunks for ALL commands in the reference."""
    return extract_command_chunks(pdf_path)


__all__ = [
    "extract_toc",
    "extract_command_chunks",
    "chunk_essential",
    "chunk_all",
    "CommandChunk",
    "SNLA_ESSENTIAL_COMMANDS",
    "COMMAND_CATEGORIES",
]
