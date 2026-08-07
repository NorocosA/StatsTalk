"""End-to-end contracts shared by the HTTP and MCP adapters."""

from __future__ import annotations

import asyncio

from snla.orchestrator import planner
from snla.ui.security import loopback_security


class _MCPContext:
    session_id = "mcp-contract"

    async def report_progress(self, *args, **kwargs):
        return None

    async def info(self, *args, **kwargs):
        return None


def _policy_view(payload):
    return {
        "method": payload["method"],
        "backend": payload["backend"],
        "statistics": payload["result"]["statistics"],
        "explanation": payload["explanation"],
        "limited_mode": payload.get("limited_mode", False),
        "audit": {
            "method": payload["audit"]["method"],
            "preferred_backend": payload["audit"]["preferred_backend"],
            "effective_backend": payload["audit"]["effective_backend"],
            "parser_used": payload["audit"]["parser_used"],
        },
    }


def test_equivalent_http_and_mcp_requests_are_policy_equivalent(
    tmp_path, sample_variables, monkeypatch
):
    from snla import config, mcp_server
    from snla.ui import server

    data_path = tmp_path / "scores.csv"
    data_path.write_text("gender,score,class,age\n1,80,A,20\n2,90,B,21\n", encoding="utf-8")
    dataset_meta = {"file_path": str(data_path), "filename": data_path.name, "row_count": 2}

    monkeypatch.setattr(config, "LLM_MOCK", True)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(mcp_server, "STATS_BACKEND", "python")
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)

    server.session.reset()
    server.session.variables = list(sample_variables)
    server.session.dataset_meta = dict(dataset_meta)
    server._executing = False

    mcp_server._session_states.clear()
    mcp_server._session_states[_MCPContext.session_id] = mcp_server.MCPState(
        variables=list(sample_variables),
        dataset_meta=dict(dataset_meta),
        file_path=str(data_path),
    )
    planner._pending.clear()

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        http_payload = client.post("/api/analyze", json={"text": "描述统计"}).get_json()

    mcp_payload = asyncio.run(mcp_server.snla_analyze(_MCPContext(), "描述统计"))

    assert _policy_view(http_payload) == _policy_view(mcp_payload)


def test_http_and_mcp_share_the_same_structured_no_data_error(monkeypatch):
    from snla import config, mcp_server
    from snla.ui import server

    monkeypatch.setattr(config, "LLM_MOCK", True)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)
    server.session.reset()
    server._executing = False
    mcp_server._session_states.clear()

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        http_response = client.post("/api/analyze", json={"text": "描述统计"})

    mcp_payload = asyncio.run(mcp_server.snla_analyze(_MCPContext(), "描述统计"))

    assert http_response.status_code == 400
    assert http_response.get_json()["error"] == mcp_payload["error"]
    assert mcp_payload["error"]["code"] == "NO_DATA"
