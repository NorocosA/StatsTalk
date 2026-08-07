"""
Regression tests — end-to-end pipeline with real-world datasets.

Ensures the full pipeline (upload → analyze → explain) doesn't crash
on datasets more complex than test_data.sav.  Uses LLM_MOCK mode so
no real LLM or SPSS is needed.

Contract: if any of these tests fail, a recent change broke the pipeline
for real users.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def setup_mock_mode():
    """Ensure LLM_MOCK is enabled for all regression tests."""
    import snla.config as cfg

    original = cfg.LLM_MOCK
    cfg.LLM_MOCK = True
    yield
    cfg.LLM_MOCK = original


@pytest.fixture(autouse=True)
def reset_session():
    """Reset session state before each test."""
    import snla.ui.server as srv

    srv.session.reset()
    srv._executing = False
    srv._active_executor = None
    srv._was_cancelled = False
    srv.planner._pending.clear()
    srv._rate_limit_store.clear()  # prevent cross-test rate limit interference
    yield


@pytest.fixture(autouse=True)
def mock_spss_executor_factory():
    """The airline regression exercises the Python path without requiring SPSS."""
    with patch("snla.executor.spss.SPSSExecutor", return_value=MagicMock()):
        yield


@pytest.fixture
def airline_meta():
    """Load airline.sav metadata (25,976 x 24)."""
    from snla.data.reader import read_and_extract

    return read_and_extract("data/fixtures/airline.sav")


def _setup_session(meta):
    """Populate the server session with dataset metadata."""
    import snla.ui.server as srv

    srv.session.dataset_meta = meta
    srv.session.variables = meta.get("variables", [])


def _call_analyze(text):
    """Call /api/analyze via Flask test client and return parsed JSON."""
    from snla.ui.security import loopback_security
    from snla.ui.server import app

    with app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        resp = client.post(
            "/api/analyze",
            json={"text": text},
            headers={"Authorization": f"Bearer {bootstrap.get_json()['session_token']}"},
        )
        return resp.status_code, json.loads(resp.data)


# ── Tests ─────────────────────────────────────────────────────────────────


class TestAirlinePipeline:
    """Full pipeline regression tests with airline.sav."""

    def test_upload_and_status(self, airline_meta):
        """Upload airline.sav — session should have 24 variables."""
        _setup_session(airline_meta)

        import snla.ui.server as srv

        assert srv.session.variables, "Session should have variables after upload"
        assert len(srv.session.variables) == 24, (
            f"Expected 24 variables, got {len(srv.session.variables)}"
        )
        # All variables must have name/type/label contract
        for v in srv.session.variables:
            assert "name" in v, f"Variable missing 'name': {v}"
            assert "type" in v, f"Variable {v.get('name')} missing 'type'"
            assert "label" in v, f"Variable {v.get('name')} missing 'label'"

    def test_analyze_descriptives(self, airline_meta):
        """Descriptives on airline.sav should not crash."""
        _setup_session(airline_meta)
        status, data = _call_analyze("显示飞行距离的描述统计")
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert data.get("ok"), f"Analysis failed: {data}"
        # Should be descriptives or a valid fallback
        assert "method" in data, f"No method in response: {data}"

    def test_analyze_t_test(self, airline_meta):
        """T-test on airline.sav should not crash."""
        _setup_session(airline_meta)
        status, data = _call_analyze("比较男性和女性的满意度是否有差异")
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert data.get("ok"), f"Analysis failed: {data}"

    def test_analyze_correlation(self, airline_meta):
        """Correlation on airline.sav should not crash (regression test for fallback bug)."""
        _setup_session(airline_meta)
        status, data = _call_analyze("研究飞行距离和满意度的关系")
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert data.get("ok"), f"Analysis failed: {data}"

    def test_analyze_anova(self, airline_meta):
        """ANOVA on airline.sav should not crash."""
        _setup_session(airline_meta)
        status, data = _call_analyze("不同舱位等级的满意度是否有差异")
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert data.get("ok"), f"Analysis failed: {data}"

    def test_greylist_flow(self, airline_meta):
        """Greylist confirmation flow should not crash on airline data."""
        _setup_session(airline_meta)
        status, data = _call_analyze("计算新变量 z_score = (FlightDistance - 1000) / 500")
        assert status == 200, f"Expected 200, got {status}: {data}"
        # Greylist should trigger confirmation (ok=False, requires_confirmation=True)
        # or fall back to descriptives if the template can't find variables
        if data.get("requires_confirmation"):
            assert not data.get("ok"), "Greylist response should have ok=False"

    def test_session_variable_contract(self, airline_meta):
        """Every variable from airline.sav must have name/type/label."""
        _setup_session(airline_meta)

        import snla.ui.server as srv

        for v in srv.session.variables:
            assert isinstance(v.get("name"), str), f"name must be str: {v}"
            assert isinstance(v.get("type"), str), f"type must be str: {v}"
            assert v.get("label") is not None, f"label must not be None: {v}"

    def test_filter_for_cloud_preserves_name_type_label(self, airline_meta):
        """filter_for_cloud must not strip name/type/label from variable dicts."""
        from snla.data.sanitizer import filter_for_cloud

        result = filter_for_cloud(airline_meta)
        variables = result.get("variables", [])
        assert variables, "filter_for_cloud should preserve variables list"
        for v in variables:
            assert "name" in v, f"filter_for_cloud dropped 'name': {v}"
            assert "type" in v, f"filter_for_cloud dropped 'type': {v}"
            assert "label" in v, f"filter_for_cloud dropped 'label': {v}"
            assert "value_labels" not in v, "filter_for_cloud should strip value_labels for privacy"
