"""Privacy sanitizer for StatsTalk.

Provides two functions:
  - filter_for_cloud: strips unsafe fields from metadata before sending to cloud LLM.
  - sanitize_variables: renames sensitive variable names (name/label substring match
    against SENSITIVE_VAR_PATTERNS) to generic var_NN placeholders.
"""

CLOUD_SAFE_FIELDS: set[str] = {
    # Top-level metadata keys
    "variables",
    "row_count",
    "column_count",
    "filename",
    # Field names within variable dicts
    "name",
    "type",
    "label",
    # NOTE: value_labels intentionally excluded — contains actual value mappings
    # (e.g., {1:"Male"}) that could leak sensitive information to cloud LLM.
    "aggregate_stats",
}

SENSITIVE_VAR_PATTERNS: list[str] = [
    # Chinese patterns (clear semantic boundaries — safe for substring match)
    "姓名",
    "身份证",
    "手机",
    "电话",
    "地址",
    "住址",
    "邮箱",
    "工号",
    "学号",
    "病历号",
    "病案号",
    "护照",
    # English patterns — use word-boundary matching to avoid false positives
    # "name" alone is too broad; use compound or specific patterns instead
    "patient_name",
    "patient_id",
    "full_name",
    "first_name",
    "last_name",
    "person_name",
    "id_card",
    "id_number",
    "social_security",
    "passport_number",
    "phone",
    "mobile",
    "cellphone",
    "telephone",
    "email",
    "e_mail",
    "address",
    "home_address",
    "mailing_address",
    "ssn",
]


def filter_for_cloud(metadata: dict) -> dict:
    """Return a new dict containing only CLOUD_SAFE_FIELDS keys present in *metadata*.

    Any keys in *metadata* that are not in CLOUD_SAFE_FIELDS (such as raw data,
    identifiers, etc.) are silently dropped.

    Additionally strips ``value_labels`` from each variable dict to prevent
    privacy leaks from actual value mappings (e.g., {1:"Male"}) being sent to cloud LLM.
    """
    result: dict = {}
    for k, v in metadata.items():
        if k in CLOUD_SAFE_FIELDS:
            if k == "variables" and isinstance(v, list):
                # Strip value_labels from each variable dict
                cleaned_vars = []
                for var in v:
                    if isinstance(var, dict):
                        cleaned = {fk: fv for fk, fv in var.items() if fk in CLOUD_SAFE_FIELDS}
                        cleaned_vars.append(cleaned)
                    else:
                        cleaned_vars.append(var)
                result[k] = cleaned_vars
            else:
                result[k] = v
    return result


def sanitize_variables(variables: list[dict]) -> tuple[list[dict], int]:
    """Desensitize variable names that match SENSITIVE_VAR_PATTERNS.

    For each variable dict in the input list, the *name* and *label* fields are
    checked (case-insensitive).  Chinese patterns (e.g. "姓名") use substring
    matching since CJK characters have clear semantic boundaries.  English
    patterns use word-boundary matching (``\\b``) to avoid false positives
    like "class" matching "name".

    Matched variables are renamed ``var_{NN}`` with ``original_name`` preserved
    and ``desensitized: True``.

    Returns:
        Tuple of (desensitized list, count of sensitive variables found).
    """
    import re

    result: list[dict] = []
    counter = 0

    # Split patterns: CJK → substring match, ASCII → word-boundary match
    cjk_patterns: list[str] = []
    ascii_patterns: list[str] = []
    for p in SENSITIVE_VAR_PATTERNS:
        if any("\u4e00" <= c <= "\u9fff" for c in p):
            cjk_patterns.append(p.lower())
        else:
            ascii_patterns.append(p.lower())

    for var in variables:
        name = var.get("name", "")
        label = var.get("label", "")
        combined = f"{name} {label}".lower()

        is_sensitive = False

        # CJK: substring match
        for pat in cjk_patterns:
            if pat in combined:
                is_sensitive = True
                break

        # ASCII: word-boundary match with separator normalization
        # Replace separators (_ . -) with spaces so "patient_id" matches "patient id"
        if not is_sensitive:
            normalized = re.sub(r"[_.\-]", " ", combined)
            for pat in ascii_patterns:
                pat_normalized = re.sub(r"[_.\-]", " ", pat)
                if re.search(r"\b" + re.escape(pat_normalized) + r"\b", normalized):
                    is_sensitive = True
                    break

        if is_sensitive:
            counter += 1
            new_var = dict(var)
            new_var["name"] = f"var_{counter:02d}"
            new_var["original_name"] = var.get("name", "")
            new_var["desensitized"] = True
            result.append(new_var)
        else:
            result.append(var)

    return result, counter
