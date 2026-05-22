"""
SNLA Orchestrator — analysis pipeline coordinator shared by Flask and MCP servers.

Provides:
  - GreylistPending, PlanResult: data classes for the planning pipeline
  - Planner: LLM-based intent/method/variable planning + greylist state machine
  - planner: module-level singleton ready for import

Design principle: Planner has zero dependency on Flask or MCP.  It is purely
functional given input variables, dataset metadata, and session context.  Both
server.py and mcp_server.py instantiate or reuse the singleton.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class PlanResult:
    """Output of the analysis planning step (Phase 1)."""
    method: str                        # e.g. "independent_t_test"
    plan_explanation: str              # human-readable plan summary
    grouping_variable: str | None      # categorical / grouping variable name
    test_variable: str | None          # numeric / outcome variable name


@dataclass
class GreylistPending:
    """Context stored while awaiting user confirmation for greylist ops.

    COMPUTE / RECODE / SELECT IF syntax requires explicit confirmation before
    execution (always on a temporary data copy).  This dataclass captures
    everything needed to re-execute once the user confirms.
    """
    syntax: str                        # validated SPSS syntax string
    warnings: list[str]                # greylist warning messages
    method: str                        # analysis method e.g. "chi_square"
    user_input: str                    # original natural-language query


# ── Exceptions ───────────────────────────────────────────────────────────

class NoPendingError(Exception):
    """Raised when confirm() is called but no greylist is pending."""


# ── Planner skeleton (implementation in planner.py) ─────────────────────

class Planner:
    """Analysis planner shared between Flask and MCP servers.

    Responsibilities:
      - LLM intent recognition + method recommendation + variable matching
      - MOCK-mode fallback with keyword-based classifier
      - Greylist state management (stage / pop / cancel per session_id)

    The ``_pending`` dict is keyed by session_id so that a single Planner
    instance can serve both the Flask server ("default") and multiple MCP
    sessions (ctx.session_id).
    """

    def __init__(self) -> None:
        self._pending: dict[str, GreylistPending] = {}

    # ── Greylist state machine ────────────────────────────────────────

    def stage_greylist(self, session_id: str, pending: GreylistPending) -> None:
        """Store a pending greylist operation for later confirmation."""
        self._pending[session_id] = pending

    def pop_pending(self, session_id: str) -> GreylistPending:
        """Pop and return the pending greylist; raise NoPendingError if none."""
        pending = self._pending.pop(session_id, None)
        if pending is None:
            raise NoPendingError("没有待确认的操作")
        return pending

    def cancel_pending(self, session_id: str) -> None:
        """Cancel the pending greylist operation (called by /api/cancel)."""
        self._pending.pop(session_id, None)

    def has_pending(self, session_id: str) -> bool:
        """Return True if a greylist operation is awaiting confirmation."""
        return session_id in self._pending

    # ── Planning (implemented in planner.py) ──────────────────────────

    def plan(
        self,
        session_id: str,
        user_input: str,
        variables: list[dict],
        dataset_meta: dict | None = None,
        last_analysis: dict | None = None,
    ) -> PlanResult:
        """Determine statistical method, explanation, and variable mapping.

        Args:
            session_id: session identifier ("default" for Flask, ctx.session_id for MCP).
            user_input: natural-language query from the user.
            variables: cloud-safe variable metadata list (from filter_for_cloud).
            dataset_meta: optional dict with keys like row_count, filename.
            last_analysis: optional dict from previous analysis (for follow-up detection).

        Returns:
            PlanResult with method, plan_explanation, grouping_variable, test_variable.
        """
        from .planner import _plan
        return _plan(
            planner=self,
            session_id=session_id,
            user_input=user_input,
            variables=variables,
            dataset_meta=dataset_meta,
            last_analysis=last_analysis,
        )


# ── Module-level singleton ───────────────────────────────────────────────

planner = Planner()
