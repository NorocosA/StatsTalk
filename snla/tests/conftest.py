"""
Shared pytest fixtures and test utilities for the SNLA test suite.

Provides reusable fixtures for:
- Sample variable definitions (validator, sanitizer, prompt tests)
- Mock SPSS OMS XML output (parser tests)
- Mock LLM responses (intent, explainer tests)
- Pre-built AnalysisResult objects (explainer, integration tests)
- Temporary file helpers
"""

import os
import tempfile

import pytest


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers",
        "slow: marks tests that require real LLM/SPSS (deselect with '-m \"not slow\"')",
    )


# ── Variable definitions ─────────────────────────────────────────────────────


@pytest.fixture
def sample_variables():
    """Returns a list of variable dicts matching the project's test_data.sav spec.

    Mirrors the dataset used by fixtures/test_data.sav:
    - gender (Numeric, with 男/女 value labels)
    - score (Numeric)
    - class (String)
    - age (Numeric)
    """
    return [
        {"name": "gender", "type": "Numeric", "label": "性别", "value_labels": {1: "男", 2: "女"}},
        {"name": "score", "type": "Numeric", "label": "考试成绩", "value_labels": None},
        {"name": "class", "type": "String", "label": "班级名", "value_labels": None},
        {"name": "age", "type": "Numeric", "label": "年龄", "value_labels": None},
    ]


@pytest.fixture
def sensitive_variables():
    """Returns variables with sensitive names for privacy/sanitizer tests.

    Contains a mix of Chinese-sensitive names (患者姓名, 手机号),
    English-pattern sensitive names (email_addr), and a normal variable (score)
    so tests can verify that only sensitive names are flagged.
    """
    return [
        {"name": "患者姓名", "type": "String", "label": "Patient Name"},
        {"name": "手机号", "type": "String", "label": "联系电话"},
        {"name": "score", "type": "Numeric", "label": "考试成绩"},
        {"name": "email_addr", "type": "String", "label": "电子邮箱"},
    ]


# ── Dataset metadata ─────────────────────────────────────────────────────────


@pytest.fixture
def dataset_meta():
    """Returns a sample dataset metadata dict matching test_data.sav."""
    return {
        "filename": "test_data.sav",
        "format": "sav",
        "row_count": 200,
        "column_count": 4,
        "file_path": "/mock/path/test_data.sav",
    }


# ── Mock SPSS output (OMS XML) ──────────────────────────────────────────────


@pytest.fixture
def mock_spss_output_ttest():
    """Returns a valid OMS XML string for T-TEST output.

    Contains two pivot tables:
    - Group Statistics (gender-stratified N, Mean, Std. Deviation)
    - Independent Samples Test (t, df, p-value)
    """
    return """<oms>
  <command text="T-TEST">
    <pivotTable text="Group Statistics">
      <dimension axis="variable"><category text="gender"/></dimension>
      <dimension axis="statistics">
        <category text="N"><cell text="10"/></category>
        <category text="Mean"><cell text="79.5"/></category>
        <category text="Std. Deviation"><cell text="8.2"/></category>
      </dimension>
    </pivotTable>
    <pivotTable text="Independent Samples Test">
      <dimension axis="statistics">
        <category text="t"><cell text="2.34"/></category>
        <category text="df"><cell text="18"/></category>
        <category text="Sig. (2-tailed)"><cell text="0.021"/></category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""


# ── Mock LLM response factory ────────────────────────────────────────────────


@pytest.fixture
def mock_llm_response():
    """Factory fixture that returns a function to create mock LLM response dicts.

    The returned callable accepts optional *content* (the response text) and
    *model* (model identifier) and returns a dict shaped like a real LLM
    provider response, including token usage metadata.

    Usage::

        def test_something(mock_llm_response):
            resp = mock_llm_response('{"intent": "describe"}')
            assert resp["content"] == '{"intent": "describe"}'
    """

    def _make_response(
        content: str = '{"intent": "describe", "confidence": 0.9}', model: str = "mock"
    ):
        return {
            "content": content,
            "model": model,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    return _make_response


# ── Temporary file helpers ───────────────────────────────────────────────────


@pytest.fixture
def temp_xml_file(mock_spss_output_ttest):
    """Create a temporary OMS XML file on disk and return its path.

    Uses the *mock_spss_output_ttest* fixture content so parser tests have
    a real file to read from.  The file is automatically cleaned up after
    the test function returns.

    Yields:
        str: Absolute path to the temporary XML file.
    """
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8")
    tmp.write(mock_spss_output_ttest)
    tmp.close()
    yield tmp.name
    os.unlink(tmp.name)


# ── Pre-built AnalysisResult fixtures ────────────────────────────────────────
# Imports are inside each fixture to avoid circular / import-before-exists errors
# when the schema module is still being developed.


@pytest.fixture
def analysis_result_ttest():
    """Returns a pre-built AnalysisResult for a *significant* T-TEST.

    Statistics: t=2.34, p=0.021, df=18 — typical of a statistically significant
    independent-samples t-test used to exercise explainer natural-language
    generation for the "significant result" code path.
    """
    from snla.parser.schema import AnalysisResult, TableResult

    return AnalysisResult(
        analysis_type="T-TEST",
        tables=[
            TableResult(
                title="Group Statistics",
                rows=[
                    {"gender": "男", "N": 10, "Mean": 79.5, "StdDev": 8.2},
                    {"gender": "女", "N": 10, "Mean": 84.2, "StdDev": 7.1},
                ],
                notes=[],
                source_format="oms_xml",
            ),
            TableResult(
                title="Independent Samples Test",
                rows=[
                    {"t": 2.34, "df": 18, "p_value": 0.021, "mean_diff": 4.7},
                ],
                notes=["Equal variances assumed"],
                source_format="oms_xml",
            ),
        ],
        statistics={"t_value": 2.34, "p_value": 0.021, "df": 18, "mean_diff": 4.7},
        n_valid=20,
        n_missing=0,
        notes=[],
        raw_output_path=None,
        parser_used="oms_xml",
    )


@pytest.fixture
def analysis_result_not_sig():
    """Returns a pre-built AnalysisResult for a *non-significant* T-TEST.

    Statistics: t=1.20, p=0.051, df=18 — just above the conventional alpha=0.05
    threshold, used to exercise the "not statistically significant" explanation
    code path.
    """
    from snla.parser.schema import AnalysisResult, TableResult

    return AnalysisResult(
        analysis_type="T-TEST",
        tables=[
            TableResult(
                title="Group Statistics",
                rows=[
                    {"gender": "男", "N": 10, "Mean": 82.1, "StdDev": 7.5},
                    {"gender": "女", "N": 10, "Mean": 84.2, "StdDev": 7.1},
                ],
                notes=[],
                source_format="oms_xml",
            ),
            TableResult(
                title="Independent Samples Test",
                rows=[
                    {"t": 1.20, "df": 18, "p_value": 0.051, "mean_diff": 2.1},
                ],
                notes=["Equal variances assumed"],
                source_format="oms_xml",
            ),
        ],
        statistics={"t_value": 1.20, "p_value": 0.051, "df": 18, "mean_diff": 2.1},
        n_valid=20,
        n_missing=0,
        notes=[],
        raw_output_path=None,
        parser_used="oms_xml",
    )


@pytest.fixture
def analysis_result_edge_sig():
    """Returns a pre-built AnalysisResult for an *edge-significance* T-TEST.

    Statistics: t=1.85, p=0.09, df=18 — a marginal result often reported as
    "trending toward significance", used to exercise boundary-condition
    handling in the explainer.
    """
    from snla.parser.schema import AnalysisResult

    return AnalysisResult(
        analysis_type="T-TEST",
        tables=[],
        statistics={"t_value": 1.85, "p_value": 0.09, "df": 18},
        n_valid=20,
        n_missing=0,
        notes=[],
        raw_output_path=None,
        parser_used="oms_xml",
    )
