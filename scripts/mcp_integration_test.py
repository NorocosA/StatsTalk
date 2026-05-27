"""
MCP Server integration test — validates the 7 MCP tools work correctly.

Run: python scripts/mcp_integration_test.py
Requires: snla package importable, project root as CWD or on sys.path.
          No SPSS or LLM needed — uses mock contexts and direct state access.
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────


def _discover_tools(mcp):
    """Discover all registered MCP tools from a FastMCP instance."""
    tm = mcp._tool_manager
    return list(tm._tools.values())


class MockContext:
    """Minimal mock of FastMCP Context for test purposes.

    Only provides what _session_state() actually needs from the real Context:
    - session_id (property, used as dict key into _session_states)
    - request_id (property, accessed for logging)
    - info() / report_progress() / debug() / error() / warning() (async no-ops,
      called by some tools for progress reporting)
    """

    def __init__(self, session_id="test-session"):
        self._session_id = session_id

    @property
    def session_id(self):
        return self._session_id

    @property
    def request_id(self):
        return "test-request"

    async def info(self, *args, **kwargs):
        pass

    async def report_progress(self, *args, **kwargs):
        pass

    async def debug(self, *args, **kwargs):
        pass

    async def error(self, *args, **kwargs):
        pass

    async def warning(self, *args, **kwargs):
        pass


# ── Tests ────────────────────────────────────────────────────────────────


def test_import():
    """Verify the MCP server module imports cleanly."""
    from snla.mcp_server import mcp as mcp_server

    assert mcp_server is not None, "MCP server (FastMCP instance) is None"
    print("[OK] MCP server module imported")


def test_tool_count():
    """Verify exactly 7 tools are registered with the FastMCP instance."""
    from snla.mcp_server import mcp as mcp_server

    tools = _discover_tools(mcp_server)
    actual = len(tools)
    names = [t.name for t in tools]
    assert actual == 7, f"Expected 7 tools, got {actual}: {names}"
    print(f"[OK] 7 tools registered: {names}")


def test_tool_names():
    """Verify all expected tool names match."""
    from snla.mcp_server import mcp as mcp_server

    expected = {
        "snla_status",
        "snla_upload",
        "snla_variables",
        "snla_analyze",
        "snla_confirm",
        "snla_cancel",
        "snla_export",
    }
    tools = _discover_tools(mcp_server)
    names = {t.name for t in tools}
    missing = expected - names
    extra = names - expected
    assert not missing, f"Missing tools: {missing}"
    assert not extra, f"Unexpected tools: {extra}"
    print("[OK] All 7 tool names verified")


def test_error_format():
    """Verify _mk_error produces structured errors matching the spec."""
    from snla.mcp_server import _mk_error

    # Full error with suggestion
    error = _mk_error(
        "VALIDATION",
        "Variable not found",
        code="VAR_NOT_FOUND",
        suggestion="Check spelling of variable names",
    )
    assert "ok" in error, "Missing 'ok' key"
    assert error["ok"] is False, "Error must have ok=False"
    assert "error" in error, "Missing 'error' key"
    err = error["error"]
    assert err["category"] == "VALIDATION"
    assert err["user_message"] == "Variable not found"
    assert err["code"] == "VAR_NOT_FOUND"
    assert err["suggestion"] == "Check spelling of variable names"

    # Minimal error (no suggestion) — suggestion should be None
    error2 = _mk_error("USER", "Simple error", code="SIMPLE")
    assert error2["error"]["suggestion"] is None

    print("[OK] _mk_error format correct")


def test_engine_busy_format():
    """Verify _engine_busy returns a standard ENGINE_BUSY error."""
    from snla.mcp_server import _engine_busy

    error = _engine_busy()
    assert error["ok"] is False
    assert error["error"]["code"] == "ENGINE_BUSY"
    assert error["error"]["category"] == "system"
    assert isinstance(error["error"]["user_message"], str)
    print("[OK] _engine_busy format correct")


def test_session_isolation():
    """Verify MCPState instances are independent per session_id."""
    from snla.mcp_server import _session_states, MCPState

    _session_states.clear()

    # Create two independent session states
    s1 = MCPState()
    s2 = MCPState()
    _session_states["session_a"] = s1
    _session_states["session_b"] = s2

    assert id(s1) != id(s2), "Sessions should be distinct objects"

    # Mutate each independently
    s1.variables = [{"name": "age"}]
    s2.variables = [{"name": "gender"}]

    assert s1.variables != s2.variables, "Sessions must not share state"
    assert _session_states["session_a"].variables[0]["name"] == "age"
    assert _session_states["session_b"].variables[0]["name"] == "gender"

    # Cleanup
    _session_states.clear()
    print("[OK] Session isolation works")


def test_status_tool():
    """Call snla_status with a mock context and verify response structure.

    This is the only tool we call directly (others require data files or
    have side effects). It validates the async-call-with-mock-ctx pattern.
    """
    from snla.mcp_server import snla_status, _session_states

    _session_states.clear()
    ctx = MockContext(session_id="test-status")

    result = asyncio.run(snla_status(ctx))

    # Must be a success
    assert result.get("ok") is True, "snla_status must return ok=True"

    # Check expected response keys
    expected_keys = {
        "ok",
        "backend",
        "spss_available",
        "trusted_methods",
        "trust_source",
        "has_data",
        "variable_count",
        "filename",
        "executing",
    }
    actual_keys = set(result.keys())
    missing = expected_keys - actual_keys
    assert not missing, f"Missing keys in snla_status response: {missing}"

    # Type checks
    assert isinstance(result["backend"], str)
    assert isinstance(result["spss_available"], bool)
    # trusted_methods may be list or set depending on backend
    assert isinstance(result["trusted_methods"], (list, set))
    assert isinstance(result["trust_source"], str)
    assert isinstance(result["has_data"], bool)
    assert isinstance(result["variable_count"], int)
    assert isinstance(result["filename"], str)
    assert isinstance(result["executing"], bool)

    # Post-conditions for fresh session
    assert result["has_data"] is False
    assert result["variable_count"] == 0
    assert result["executing"] is False

    _session_states.clear()
    print("[OK] snla_status returns correct structure")


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    tests = [
        ("import", test_import),
        ("tool_count", test_tool_count),
        ("tool_names", test_tool_names),
        ("error_format", test_error_format),
        ("engine_busy_format", test_engine_busy_format),
        ("session_isolation", test_session_isolation),
        ("status_tool", test_status_tool),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        label = f"[{name}]".ljust(16)
        try:
            fn()
            print(f"{label} PASS")
            passed += 1
        except Exception as e:
            print(f"{label} FAIL")
            print(f"  {e}")
            import traceback

            traceback.print_exc(limit=3)
            failed += 1

    total = passed + failed
    print()
    print("-" * 40)
    print(f"  {total} tests: {passed} passed, {failed} failed")
    print("-" * 40)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
