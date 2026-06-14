"""
Variable range expander — detects and expands variable range patterns
like "Q1-Q10" or "age-Q1 to age-Q10" into explicit variable lists.

Used as a pre-processor before the LLM planner so the LLM never sees
range syntax — it only sees explicit variable names.
"""

import re


def detect_range_pattern(user_input: str) -> tuple[str, str, str, str] | None:
    """Detect variable range patterns in user input.

    Returns (prefix, start_num, end_num, suffix) if found, None otherwise.

    Examples:
        "分析Q1-Q10" → ("Q", "1", "10", "")
        "比较Q01到Q20" → ("Q", "01", "20", "")
        "查看item_1至item_5" → ("item_", "1", "5", "")
    """
    # Pattern: prefix + digits + (separator) + prefix? + digits + suffix
    patterns = [
        (r"(\w+?)(\d+)\s*[-–—至到]\s*\1?(\d+)(\b|$)", True),  # Q1-Q10, item_1至item_5
        (r"(\w+?)(\d+)\s*[-–—至到]\s*(\d+)(\b|$)", False),  # 1-10 (no prefix)
    ]

    for pattern, has_prefix in patterns:
        match = re.search(pattern, user_input)
        if match:
            if has_prefix:
                prefix, start_num, end_num, _ = match.groups()
            else:
                prefix, start_num, end_num, _ = (
                    match.groups() if len(match.groups()) >= 4 else match.groups() + ("",)
                )
            return (prefix, start_num, end_num, "")
    return None


def expand_range(prefix: str, start: str, end: str, available_variables: list[str]) -> list[str]:
    """Expand a variable range against actual available variables.

    Args:
        prefix: Variable name prefix (e.g., "Q")
        start: Starting number string (e.g., "1" or "01")
        end: Ending number string (e.g., "10")
        available_variables: List of actual variable names from the dataset

    Returns:
        List of matching variable names, sorted.
    """
    start_num = int(start)
    end_num = int(end)
    num_width = len(start)  # preserve zero-padding width

    if start_num > end_num:
        start_num, end_num = end_num, start_num

    # Build expected variable names
    expanded = []
    for i in range(start_num, end_num + 1):
        var_name = f"{prefix}{i}"
        # Also try zero-padded variants
        padded = f"{prefix}{str(i).zfill(num_width)}"

        if var_name in available_variables:
            expanded.append(var_name)
        elif padded in available_variables:
            expanded.append(padded)

    return expanded


def expand_query(user_input: str, available_variables: list[str]) -> str:
    """Expand range patterns in user query, returning modified query text.

    If a range is detected and expanded, replaces the range pattern
    with explicit variable names and appends a hint.

    Args:
        user_input: Original user query
        available_variables: List of variable names from the dataset

    Returns:
        Modified query with expanded variables, or original if no range found.
    """
    detected = detect_range_pattern(user_input)
    if not detected:
        return user_input

    prefix, start, end, _ = detected
    expanded = expand_range(prefix, start, end, available_variables)

    if not expanded or len(expanded) < 2:
        return user_input  # Range didn't match any real variables

    # Replace the range pattern in the query
    var_list = ", ".join(expanded)
    modified = re.sub(r"\w+\d+\s*[-–—至到]\s*\w+\d+", var_list, user_input, count=1)
    return modified
