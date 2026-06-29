from __future__ import annotations

from snla.explainer.export import export_to_docx
from snla.parser.schema import AnalysisResult, TableResult


def test_export_to_docx_accepts_analysis_result(tmp_path):
    result = AnalysisResult(
        analysis_type="T-TEST",
        tables=[TableResult(title="Independent Samples Test", rows=[{"t": 2.34, "p": 0.021}])],
        statistics={"t_value": 2.34, "df": 18, "p_value": 0.021, "n_valid": 20},
        n_valid=20,
    )

    output = export_to_docx(
        output_path=str(tmp_path / "report.docx"),
        user_query="比较两组成绩差异",
        method="independent_t_test",
        analysis_result=result,
        explanation="两组之间存在显著差异。",
        data_file="test.sav",
    )

    assert output.endswith("report.docx")
    assert (tmp_path / "report.docx").is_file()
    assert (tmp_path / "report.docx").stat().st_size > 0


def test_export_to_docx_accepts_dict_result(tmp_path):
    result = {
        "analysis_type": "DESCRIPTIVES",
        "tables": [{"title": "Descriptive Statistics", "rows": [{"Mean": 81.2}]}],
        "statistics": {"mean": 81.2, "std_dev": 6.4, "n_valid": 30},
        "n_valid": 30,
    }

    export_to_docx(
        output_path=str(tmp_path / "dict-report.docx"),
        user_query="描述成绩",
        method="descriptives",
        analysis_result=result,
        explanation="平均分为 81.2。",
    )

    assert (tmp_path / "dict-report.docx").is_file()
