"""Boundary tests for variable-range preprocessing."""

import pytest

from snla.data.range_expander import detect_range_pattern, expand_query, expand_range


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("分析 Q1-Q10", ("Q", "1", "10", "")),
        ("分析 Q01 到 Q03", ("Q", "01", "03", "")),
        ("查看 item_1至item_5", ("item_", "1", "5", "")),
        ("没有范围", None),
        ("", None),
    ),
)
def test_detect_range_pattern(query, expected):
    assert detect_range_pattern(query) == expected


def test_expand_range_preserves_padding_and_uses_only_existing_variables():
    variables = ["Q01", "Q02", "Q04", "Q3"]

    assert expand_range("Q", "01", "04", variables) == ["Q01", "Q02", "Q3", "Q04"]


def test_expand_range_accepts_descending_bounds():
    assert expand_range("Q", "3", "1", ["Q1", "Q2", "Q3"]) == ["Q1", "Q2", "Q3"]


def test_expand_query_replaces_a_matching_range():
    assert expand_query("分析 Q1-Q3 的均值", ["Q1", "Q2", "Q3"]) == "分析 Q1, Q2, Q3 的均值"


@pytest.mark.parametrize(
    ("query", "variables"),
    (
        ("没有范围", ["Q1", "Q2"]),
        ("分析 Q1-Q3", ["Q1"]),
        ("分析 Q1-Q3", ["age", "score"]),
    ),
)
def test_expand_query_keeps_unmatched_or_incomplete_ranges(query, variables):
    assert expand_query(query, variables) == query
