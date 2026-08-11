"""Privacy sanitizer for StatsTalk.

Provides two functions:
  - filter_for_cloud: strips unsafe fields from metadata before sending to cloud LLM.
  - sanitize_variables: renames sensitive variable names (name/label substring match
    against SENSITIVE_VAR_PATTERNS) to generic var_NN placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLOUD_SAFE_METADATA_FIELDS: set[str] = {
    "variables",
    "row_count",
    "column_count",
}

CLOUD_SAFE_VARIABLE_FIELDS: set[str] = {
    "name",
    "type",
    "label",
    "role_type",
}

CLOUD_SAFE_FIELDS = CLOUD_SAFE_METADATA_FIELDS | CLOUD_SAFE_VARIABLE_FIELDS

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


@dataclass(frozen=True)
class CloudPlanningContext:
    """Approved planning metadata plus reversible sensitive-name aliases."""

    variables: list[dict]
    cloud_to_local: dict[str, str]
    sensitive_aliases: dict[str, str]

    def sanitize_text(self, text: str) -> str:
        result = text
        for original, placeholder in sorted(
            self.sensitive_aliases.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if original:
                result = re.sub(re.escape(original), placeholder, result, flags=re.IGNORECASE)
        return result

    def restore_reference(self, reference: object) -> str | None:
        if not isinstance(reference, str) or not reference.strip():
            return None
        names = [item.strip() for item in reference.split(",")]
        return ", ".join(self.cloud_to_local.get(name, name) for name in names if name)

    def restore_text(self, text: object) -> str:
        if not isinstance(text, str):
            return ""
        result = text
        for placeholder, original in sorted(
            self.cloud_to_local.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            result = re.sub(rf"\b{re.escape(placeholder)}\b", original, result)
        return result


def filter_for_cloud(metadata: dict) -> dict:
    """Return a new dict containing only CLOUD_SAFE_FIELDS keys present in *metadata*.

    Any keys in *metadata* that are not in CLOUD_SAFE_FIELDS (such as raw data,
    identifiers, etc.) are silently dropped.

    Additionally strips ``value_labels`` from each variable dict to prevent
    privacy leaks from actual value mappings (e.g., {1:"Male"}) being sent to cloud LLM.
    """
    result: dict = {}
    for key in ("row_count", "column_count"):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    variables = metadata.get("variables")
    if isinstance(variables, list):
        cleaned_vars = []
        for variable in variables:
            if not isinstance(variable, dict):
                continue
            cleaned = {
                key: value
                for key, value in variable.items()
                if key in CLOUD_SAFE_VARIABLE_FIELDS and isinstance(value, str)
            }
            if not cleaned.get("name") or not cleaned.get("type"):
                continue
            if variable.get("type") == "String" or bool(variable.get("value_labels")):
                cleaned["role_type"] = "categorical"
            elif variable.get("type") == "Numeric":
                cleaned["role_type"] = "continuous"
            else:
                cleaned["role_type"] = "unsupported"
            cleaned_vars.append(cleaned)
        result["variables"] = cleaned_vars
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
            new_var["original_label"] = var.get("label", "")
            new_var["label"] = f"Sensitive variable {counter:02d}"
            new_var["desensitized"] = True
            result.append(new_var)
        else:
            result.append(var)

    return result, counter


def build_cloud_planning_context(variables: list[dict]) -> CloudPlanningContext:
    """Create the only variable representation allowed in planning calls."""

    sanitized, _count = sanitize_variables(variables)
    cloud_to_local: dict[str, str] = {}
    sensitive_aliases: dict[str, str] = {}
    for variable in sanitized:
        placeholder = variable.get("name")
        original_name = variable.get("original_name")
        if not isinstance(placeholder, str) or not isinstance(original_name, str):
            continue
        cloud_to_local[placeholder] = original_name
        sensitive_aliases[original_name] = placeholder
        original_label = variable.get("original_label")
        if isinstance(original_label, str) and original_label:
            sensitive_aliases[original_label] = placeholder

    safe_variables = filter_for_cloud({"variables": sanitized}).get("variables", [])
    return CloudPlanningContext(
        variables=safe_variables,
        cloud_to_local=cloud_to_local,
        sensitive_aliases=sensitive_aliases,
    )
