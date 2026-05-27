"""Tests for snla.syntax.validator — 11 test cases covering blacklist, greylist,
variable validation, bracket checking, and clean syntax pass-through."""

from snla.syntax.validator import validate

# ── Blacklist tests (valid=False, errors contain command name) ──────────────


def test_blacklist_blocks_save():
    """SAVE OUTFILE is blacklisted → valid=False, error mentions SAVE."""
    result = validate("SAVE OUTFILE='test.sav'.")
    assert result["valid"] is False
    assert any("SAVE" in e for e in result["errors"]), (
        f"Expected error mentioning 'SAVE', got errors={result['errors']}"
    )


def test_blacklist_blocks_delete():
    """DELETE VARIABLES is blacklisted → valid=False, error mentions DELETE."""
    result = validate("DELETE VARIABLES=gender.")
    assert result["valid"] is False
    assert any("DELETE" in e for e in result["errors"]), (
        f"Expected error mentioning 'DELETE', got errors={result['errors']}"
    )


def test_blacklist_blocks_aggregate():
    """AGGREGATE with OUTFILE=* is blacklisted → valid=False, error mentions AGGREGATE."""
    result = validate("AGGREGATE /OUTFILE=* /BREAK=gender.")
    assert result["valid"] is False
    assert any("AGGREGATE" in e for e in result["errors"]), (
        f"Expected error mentioning 'AGGREGATE', got errors={result['errors']}"
    )


def test_blacklist_blocks_add_files():
    """ADD FILES is blacklisted → valid=False, error mentions ADD FILES."""
    result = validate("ADD FILES /FILE=*.")
    assert result["valid"] is False
    assert any("ADD FILES" in e.upper() for e in result["errors"]), (
        f"Expected error mentioning 'ADD FILES', got errors={result['errors']}"
    )


def test_blacklist_blocks_begin_program():
    """BEGIN PROGRAM PYTHON is blacklisted → valid=False, error mentions BEGIN PROGRAM."""
    result = validate("BEGIN PROGRAM PYTHON.")
    assert result["valid"] is False
    assert any("BEGIN PROGRAM" in e.upper() for e in result["errors"]), (
        f"Expected error mentioning 'BEGIN PROGRAM', got errors={result['errors']}"
    )


# ── Greylist tests (valid=True, warnings contain command name) ─────────────


def test_greylist_compute_requires_confirmation():
    """COMPUTE is greylisted → valid=True, warning mentions COMPUTE."""
    result = validate("COMPUTE newvar = oldvar * 2.")
    assert result["valid"] is True, (
        f"Greylisted commands should pass validation, got errors={result['errors']}"
    )
    assert any("COMPUTE" in w for w in result["warnings"]), (
        f"Expected warning mentioning 'COMPUTE', got warnings={result['warnings']}"
    )


def test_greylist_recode_requires_confirmation():
    """RECODE is greylisted → valid=True, warning mentions RECODE."""
    result = validate("RECODE gender (1=0)(2=1).")
    assert result["valid"] is True, (
        f"Greylisted commands should pass validation, got errors={result['errors']}"
    )
    assert any("RECODE" in w for w in result["warnings"]), (
        f"Expected warning mentioning 'RECODE', got warnings={result['warnings']}"
    )


def test_greylist_select_if_requires_confirmation():
    """SELECT IF is greylisted → valid=True, warning mentions SELECT IF."""
    result = validate("SELECT IF (gender = 1).")
    assert result["valid"] is True, (
        f"Greylisted commands should pass validation, got errors={result['errors']}"
    )
    assert any("SELECT IF" in w.upper() for w in result["warnings"]), (
        f"Expected warning mentioning 'SELECT IF', got warnings={result['warnings']}"
    )


# ── Variable validation test ───────────────────────────────────────────────


def test_variable_not_exists():
    """Referencing a variable not in the dataset → valid=False, error mentions it."""
    result = validate(
        "T-TEST GROUPS=gender(1 2) /VARIABLES=nonexistent.",
        var_list=["gender", "score"],
    )
    assert result["valid"] is False
    assert any("nonexistent" in e for e in result["errors"]), (
        f"Expected error mentioning 'nonexistent', got errors={result['errors']}"
    )


# ── Bracket / parenthesis mismatch test ────────────────────────────────────


def test_bracket_mismatch():
    """Unmatched opening parenthesis → valid=False, error mentions bracket or parenthesis."""
    result = validate("COMPUTE x = (a + b.")
    assert result["valid"] is False
    assert any(
        keyword in e.lower()
        for e in result["errors"]
        for keyword in ("bracket", "parenthesis", "parentheses", "mismatch", "unmatched")
    ), f"Expected error mentioning bracket/parenthesis mismatch, got errors={result['errors']}"


# ── Clean syntax pass-through test ─────────────────────────────────────────


def test_clean_syntax_passes():
    """Safe syntax with valid variables → valid=True, no errors, no warnings."""
    result = validate(
        "FREQUENCIES VARIABLES=gender.",
        var_list=["gender", "score"],
    )
    assert result["valid"] is True, f"Clean syntax should pass, got errors={result['errors']}"
    assert len(result["errors"]) == 0, (
        f"Clean syntax should produce no errors, got {result['errors']}"
    )
    assert len(result["warnings"]) == 0, (
        f"Clean syntax should produce no warnings, got {result['warnings']}"
    )
