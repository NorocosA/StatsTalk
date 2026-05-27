"""SPSS Syntax Security Validator.

This module provides a security sandbox for validating SPSS syntax before
execution. It performs the following checks:

1. **Blacklist check** -- blocks commands that modify files, delete data, or
   execute system-level operations (e.g. SAVE, DELETE, HOST COMMAND).
2. **Greylist check** -- flags commands that modify in-memory data and require
   explicit user confirmation (e.g. COMPUTE, RECODE, SELECT IF).
3. **Variable validation** -- verifies that every variable referenced in the
   syntax exists in the user's known variable list.
4. **Bracket / quote pairing** -- ensures parentheses and single-quote pairs
   are properly balanced.

Usage:
    from snla.syntax.validator import validate

    result = validate("COMPUTE newvar = oldvar * 2.", var_list=["oldvar"])
    if result["valid"]:
        print("Syntax is safe.")
    else:
        print("Errors:", result["errors"])
        print("Warnings:", result["warnings"])
"""

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORBIDDEN_KEYWORDS: list[str] = [
    "SAVE",
    "XSAVE",
    "SAVE TRANSLATE",
    "EXPORT",
    "DELETE",
    "ERASE",
    "HOST COMMAND",
    "DATASET CLOSE",
    "DATASET ACTIVATE",
    "NEW FILE",
    "BEGIN PROGRAM",
    "BEGIN PROGRAM PYTHON",
    "AGGREGATE",
    "ADD FILES",
    "MATCH FILES",
]

GREYLIST_KEYWORDS: list[str] = [
    "COMPUTE",
    "RECODE",
    "SELECT IF",
    "FILTER",
    "RENAME VARIABLES",
    "SORT CASES",
    "WEIGHT",
]

# Multi-word commands (both forbidden and greylisted) sorted by length
# descending so that the longest prefix is checked first, ensuring
# "BEGIN PROGRAM PYTHON" is matched before "BEGIN PROGRAM".
_MULTI_WORD_CMDS: list[str] = sorted(
    [kw for kw in FORBIDDEN_KEYWORDS + GREYLIST_KEYWORDS if " " in kw],
    key=len,
    reverse=True,
)

# SPSS keywords that are not variable names (used during variable extraction).
_SPSS_NON_VAR_KEYWORDS: frozenset = frozenset(
    {
        "BY",
        "TO",
        "AND",
        "OR",
        "WITH",
        "ALL",
        "EQ",
        "NE",
        "LT",
        "GT",
        "LE",
        "GE",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _protect_strings(text: str, replacement: str = " ") -> str:
    """Replace single-quoted and double-quoted string literals with a placeholder.

    This prevents special characters (periods, parentheses, etc.) inside quoted
    strings from being interpreted as syntax by downstream checks.

    Args:
        text: The original SPSS syntax string.
        replacement: Placeholder string (default: single space).

    Returns:
        Syntax string with quoted contents replaced by the placeholder.
    """
    result = re.sub(r"'[^']*'", replacement, text)
    result = re.sub(r'"[^"]*"', replacement, result)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_commands(syntax: str) -> list[str]:
    """Parse SPSS syntax and extract command keywords.

    SPSS commands are terminated by a period (``.``) and the command keyword
    appears at the start of each statement. Multi-word commands (e.g.
    ``BEGIN PROGRAM PYTHON``, ``SELECT IF``) are matched greedily before
    single-word fallback.

    Args:
        syntax: A string containing one or more SPSS commands.

    Returns:
        A list of command keywords (uppercase) found in the syntax.
        Returns an empty list for empty or whitespace-only input.

    Examples:
        >>> extract_commands("T-TEST GROUPS=gender(1 2) /VARIABLES=score.")
        ["T-TEST"]

        >>> extract_commands("SAVE OUTFILE='data.sav'.\\nCOMPUTE x = y.")
        ["SAVE", "COMPUTE"]

        >>> extract_commands("BEGIN PROGRAM PYTHON.\\nprint('hello')\\nEND PROGRAM.")
        ["BEGIN PROGRAM PYTHON"]
    """
    if not syntax or not syntax.strip():
        return []

    # Protect quoted strings so periods inside them don't cause false splits
    protected = _protect_strings(syntax)

    commands: list[str] = []
    statements = protected.split(".")

    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue

        upper = stmt.upper()

        # 1) Check multi-word commands first (longest-first for greedy match)
        matched = False
        for mw in _MULTI_WORD_CMDS:
            # Ensure the match is at a word boundary (the keyword is followed
            # by a space, delimiter, or end-of-statement).
            if upper.startswith(mw) and (len(upper) == len(mw) or not upper[len(mw)].isalnum()):
                commands.append(mw)
                matched = True
                break

        if matched:
            continue

        # 2) Fallback: single-word command
        tokens = stmt.split()
        if tokens:
            first = tokens[0].rstrip(":").upper()
            commands.append(first)

    return commands


def check_blacklist(commands: list[str]) -> list[str]:
    """Return a list of blacklisted commands found in *commands*.

    Blacklisted commands are those that can modify files on disk, delete data,
    or execute operating-system commands. They should never be allowed.

    Args:
        commands: A list of command keywords (typically from
            :func:`extract_commands`).

    Returns:
        A sorted list of unique blacklisted keywords that were matched
        (empty list if none).
    """
    blacklisted: list[str] = []
    for cmd in commands:
        if cmd in FORBIDDEN_KEYWORDS:
            blacklisted.append(cmd)
    return sorted(set(blacklisted))


def check_greylist(commands: list[str]) -> list[str]:
    """Return a list of greylisted commands found in *commands*.

    Greylisted commands modify in-memory data (create new variables, recode,
    filter cases, etc.). They are permitted but SHOULD be confirmed by the
    user before execution.

    Args:
        commands: A list of command keywords (typically from
            :func:`extract_commands`).

    Returns:
        A sorted list of unique greylisted keywords that were matched
        (empty list if none).
    """
    greylisted: list[str] = []
    for cmd in commands:
        if cmd in GREYLIST_KEYWORDS:
            greylisted.append(cmd)
    return sorted(set(greylisted))


def validate_variables(syntax: str, var_list: list[str]) -> list[str]:
    """Find variable references in *syntax* and validate them against *var_list*.

    Variable references are detected via SPSS subcommand patterns:
    ``VARIABLES=``, ``GROUPS=``, ``/VARIABLES=``, ``/DEPENDENT=``,
    ``/METHOD=ENTER``, ``BY``, and ``TABLES=``.

    The check is case-insensitive. Comparison against *var_list* is also
    case-insensitive.

    Args:
        syntax: The raw SPSS syntax string.
        var_list: A list of known / existing variable names. If empty,
            an empty list is returned (no variables to validate against).

    Returns:
        A sorted list of unique variable names referenced in the syntax
        that do **not** exist in *var_list*.
    """
    if not var_list:
        return []

    # Pre-process: remove line-level comments and protect quoted strings
    processed = syntax
    processed = re.sub(r"^\s*\*.*$", "", processed, flags=re.MULTILINE)
    processed = _protect_strings(processed)
    processed = re.sub(r"\s+", " ", processed).strip()

    # Normalise variable list for case-insensitive comparison
    var_lower: dict[str, str] = {v.lower(): v for v in var_list}

    referenced: set[str] = set()

    # ---- Pattern 1: keyword=value ---------------------------------------
    # Matches VARIABLES=, GROUPS=, TABLES=, DEPENDENT= followed by values
    # up to the next / subcommand or end-of-string.
    kw_pattern = re.compile(
        r"(?:VARIABLES|GROUPS|TABLES|DEPENDENT)\s*=\s*"
        r"([^/]+?)(?=\s*/|$)",
        re.IGNORECASE,
    )
    for match in kw_pattern.finditer(processed):
        values = match.group(1).strip()
        _extract_tokens(values, referenced)

    # ---- Pattern 2: /METHOD=ENTER ... -----------------------------------
    method_pattern = re.compile(
        r"/METHOD\s*=\s*ENTER\b\s*([^/]+?)(?=\s*/|$)",
        re.IGNORECASE,
    )
    for match in method_pattern.finditer(processed):
        values = match.group(1).strip()
        _extract_tokens(values, referenced)

    # ---- Pattern 3: standalone BY keyword --------------------------------
    # 1-2 tokens surrounding BY (in context) are treated as variable refs.
    for match in re.finditer(r"\bBY\b", processed, re.IGNORECASE):
        # Grab a window of text around BY
        start = max(0, match.start() - 80)
        end = min(len(processed), match.end() + 80)
        context = processed[start:end]
        words = re.findall(r"[A-Za-z_]\w*", context)
        try:
            idx = next(i for i, w in enumerate(words) if w.upper() == "BY")
            if idx > 0:
                referenced.add(words[idx - 1])
            if idx < len(words) - 1:
                referenced.add(words[idx + 1])
        except StopIteration:
            pass

    # ---- Comparison ------------------------------------------------------
    missing: list[str] = []
    for var in referenced:
        if var.lower() not in var_lower:
            missing.append(var)

    return sorted(set(missing))


def _extract_tokens(values: str, accumulator: set[str]) -> None:
    """Split a value string into tokens and add variable-like names to *accumulator*.

    Tokens that are numeric literals, SPSS keywords, or empty are skipped.
    Variable names are added **as they appear** (preserving original casing).

    Args:
        values: The value portion extracted from a subcommand assignment
            (e.g. ``"gender(1 2)"`` or ``"score age income"``).
        accumulator: A set to which identified variable names are added.
    """
    for token in re.split(r"[\s,()]+", values):
        token = token.strip().rstrip(".")
        if not token:
            continue
        if token.isdigit():
            continue
        if token.upper() in _SPSS_NON_VAR_KEYWORDS:
            continue
        # Skip tokens that look like string literals (e.g. after protection)
        if token in ("'", '""', "''"):
            continue
        accumulator.add(token)


def check_brackets(syntax: str) -> list[str]:
    """Check parentheses ``()`` and single-quote ``'...'`` pairing in *syntax*.

    Performs a character-by-character scan that correctly handles:
    - Escaped single quotes (``''``) inside single-quoted strings.
    - Parentheses inside string literals (they are not counted).

    Args:
        syntax: The raw SPSS syntax string.

    Returns:
        A list of human-readable error descriptions. Returns an empty list
        when all brackets and quotes are properly balanced.

    Examples:
        >>> check_brackets("T-TEST GROUPS=gender(1 2).")
        []

        >>> check_brackets("COMPUTE x = (y + (z.")
        ["Unmatched opening parenthesis '(' at position 15"]

        >>> check_brackets("SAVE OUTFILE='data.sav'.")
        []
    """
    errors: list[str] = []
    paren_stack: list[int] = []  # stores character positions of '('
    in_squote = False
    i = 0

    while i < len(syntax):
        ch = syntax[i]

        if ch == "'":
            if not in_squote:
                in_squote = True
            else:
                # Inside a single-quoted string; check for escaped quote (doubled)
                if i + 1 < len(syntax) and syntax[i + 1] == "'":
                    # This is an escaped literal quote -- skip the second '
                    i += 1
                else:
                    in_squote = False

        elif not in_squote:
            if ch == "(":
                paren_stack.append(i)
            elif ch == ")":
                if paren_stack:
                    paren_stack.pop()
                else:
                    errors.append(f"Unmatched closing parenthesis ')' at position {i}")

        i += 1

    # Any remaining open parentheses
    for pos in paren_stack:
        errors.append(f"Unmatched opening parenthesis '(' at position {pos}")

    # Unmatched single quote
    if in_squote:
        errors.append("Unmatched single quote")

    return errors


def validate(
    syntax: str,
    var_list: list[str] | None = None,
) -> dict[str, object]:
    """Run all security checks on *syntax* and return a unified result dict.

    This is the main public API. It executes every validation in order:

    1. **Blacklist** -- any match produces an ``error`` (``valid=False``).
    2. **Greylist** -- any match produces a ``warning`` (``valid=True``, but
       the caller SHOULD prompt for confirmation).
    3. **Variable validation** -- missing variables produce an ``error``.
       Skipped when *var_list* is ``None`` or empty.
    4. **Bracket / quote check** -- mismatches produce an ``error``.

    Args:
        syntax: SPSS syntax to validate.
        var_list: Optional list of known variable names. Pass ``None`` or an
            empty list to skip variable validation while still performing the
            other checks.

    Returns:
        A dictionary with the following keys:

        - ``"valid"``: ``True`` when there are zero errors.
        - ``"errors"``: list of human-readable error messages (blocking).
        - ``"warnings"``: list of human-readable warning messages (non-blocking
          but SHOULD be presented to the user).
    """
    errors: list[str] = []
    warnings: list[str] = []

    commands = extract_commands(syntax)

    # 1) Blacklist check ---------------------------------------------------
    blacklisted = check_blacklist(commands)
    for cmd in blacklisted:
        errors.append(f"Blacklisted command '{cmd}' is not allowed")

    # 2) Greylist check ----------------------------------------------------
    greylisted = check_greylist(commands)
    for cmd in greylisted:
        warnings.append(f"Greylisted command '{cmd}' requires user confirmation")

    # 3) Variable validation -----------------------------------------------
    if var_list is not None:
        missing_vars = validate_variables(syntax, var_list)
        for var in missing_vars:
            errors.append(f"Unknown variable '{var}' referenced in syntax")

    # 4) Bracket / quote check ---------------------------------------------
    bracket_errors = check_brackets(syntax)
    errors.extend(bracket_errors)

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
