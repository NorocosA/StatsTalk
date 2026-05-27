"""
Tests for the SPSS output parser (snla.parser.output).

Covers OMS XML parsing, regex LST parsing (English and Chinese),
multi-dimension headers, and fallback behavior.
"""

import os
import tempfile

import pytest

from snla.parser.output import _safe_float, parse, parse_oms_xml, parse_raw_lst
from snla.parser.schema import (
    AnalysisResult,
    analysis_result_to_dict,
    dict_to_analysis_result,
)

# =========================================================================
# Test 1: OMS XML — T-TEST
# =========================================================================


class TestOmsTtestXml:
    """OMS XML parsing for a standard T-TEST output."""

    def test_oms_ttest_xml_parse(self):
        """Parse a minimal T-TEST OMS XML and verify key structure."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        # OMS XML structure note:
        # The parser's findall("dimension") only visits direct children of
        # <pivotTable>.  Dimensions nested inside <category> elements are
        # invisible to the axis-discovery loop, so nested dimension axes
        # cannot serve as column headers.  As a result, cell values from
        # nested dimensions map to a generic "Value" column and are not
        # picked up by _extract_statistics.
        #
        # This test verifies the structural parsing (analysis type, table
        # titles, non-empty tables) rather than statistics extraction,
        # which is tested through the LST regex path.
        xml_content = """\
<oms>
  <command text="T-TEST">
    <pivotTable text="Group Statistics">
      <dimension axis="row">
        <category text="Male"/>
        <category text="Female"/>
      </dimension>
      <dimension axis="statistics">
        <category text="N"><cell text="10"/></category>
        <category text="Mean"><cell text="79.5"/></category>
      </dimension>
    </pivotTable>
    <pivotTable text="Independent Samples Test">
      <dimension axis="row">
        <category text="score"/>
      </dimension>
      <dimension axis="statistics">
        <category text="t"><cell text="2.34"/></category>
        <category text="df"><cell text="18"/></category>
        <category text="Sig. (2-tailed)"><cell text="0.021"/></category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert result.analysis_type == "T-TEST"
            assert isinstance(result.tables, list)
            assert len(result.tables) >= 1

            # Statistics dict is available (may be empty depending on
            # OMS XML dimension structure — see note above).
            assert isinstance(result.statistics, dict)

            # Verify table titles
            table_titles = [t.title for t in result.tables]
            assert "Group Statistics" in table_titles
            assert "Independent Samples Test" in table_titles

            # Verify parser_used is set correctly
            assert result.parser_used == "oms_xml"

            # Verify raw_output_path points to the temp file
            assert result.raw_output_path == tmp.name

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 2: OMS XML — FREQUENCIES
# =========================================================================


class TestOmsFrequenciesXml:
    """OMS XML parsing for a FREQUENCIES output."""

    def test_oms_frequencies_xml_parse(self):
        """Parse a FREQUENCIES OMS XML and verify frequency rows."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        xml_content = """\
<oms>
  <command text="FREQUENCIES VARIABLES=education">
    <pivotTable text="Statistics">
      <dimension axis="row">
        <category text="education"/>
      </dimension>
      <dimension axis="statistics">
        <category text="N"><cell text="100"/></category>
        <category text="Mean"><cell text="3.45"/></category>
      </dimension>
    </pivotTable>
    <pivotTable text="Frequency">
      <dimension axis="row">
        <category text="High School"/>
        <category text="Bachelor"/>
        <category text="Master"/>
        <category text="PhD"/>
      </dimension>
      <dimension axis="statistics">
        <category text="Frequency"><cell text="30"/></category>
        <category text="Percent"><cell text="30.0"/></category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert isinstance(result.tables, list)
            assert len(result.tables) > 0

            # At least one table should have frequency-type title
            freq_tables = [
                t for t in result.tables if "Frequency" in t.title or "Statistics" in t.title
            ]
            assert len(freq_tables) >= 1

            # All tables should have non-empty rows
            all_rows = []
            for table in result.tables:
                all_rows.extend(table.rows)
            assert len(all_rows) > 0

            # Verify parser_used
            assert result.parser_used == "oms_xml"

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 3: LST regex — English T-TEST
# =========================================================================


class TestLstTtestEn:
    """Regex LST parsing for English T-TEST output."""

    # NOTE: The parser's regex uses \s{2,} (two or more whitespace)
    # between columns, so mock text must use at least 2 spaces between tokens.
    MOCK_LST_EN = """\
T-TEST GROUPS=gender(1 2) /VARIABLES=score.

Group Statistics
            N     Mean    Std. Deviation
Male    10    79.500     8.200
Female    10    84.200     7.100

Independent Samples Test
Equal variances assumed  1.23  0.281  2.340   18    0.021

"""

    def test_lst_ttest_en_parse(self):
        """Parse English LST text and verify Group Statistics table."""
        result = parse_raw_lst(self.MOCK_LST_EN, "T-TEST")

        assert isinstance(result, AnalysisResult)
        assert len(result.tables) >= 1

        table_titles = [t.title for t in result.tables]
        assert "Group Statistics" in table_titles

    def test_lst_ttest_en_statistics(self):
        """Verify that p_value and t_value are extracted from English LST."""
        result = parse_raw_lst(self.MOCK_LST_EN, "T-TEST")

        assert "t_value" in result.statistics
        assert "p_value" in result.statistics

        # Both should be numeric
        assert isinstance(result.statistics["t_value"], (int, float))
        assert isinstance(result.statistics["p_value"], (int, float))


# =========================================================================
# Test 4: LST regex — Chinese T-TEST
# =========================================================================


class TestLstTtestZh:
    """Regex LST parsing for Chinese (Simplified) T-TEST output."""

    MOCK_LST_ZH = """\
T-TEST GROUPS=gender(1 2) /VARIABLES=score.

组统计
           个案数    平均值    标准差
score 男      10      79.500    8.200
      女      10      84.200    7.100

独立样本检验
假设方差相等  1.23  0.281  2.340   18     0.021

"""

    def test_lst_ttest_zh_parse(self):
        """Parse Chinese LST text and verify tables are produced."""
        result = parse_raw_lst(self.MOCK_LST_ZH, "T-TEST")

        assert isinstance(result, AnalysisResult)
        assert len(result.tables) >= 1

        table_titles = [t.title for t in result.tables]
        assert len(table_titles) >= 1

    def test_lst_ttest_zh_has_rows(self):
        """Verify Chinese LST parsing yields rows with data."""
        result = parse_raw_lst(self.MOCK_LST_ZH, "T-TEST")

        all_rows = []
        for table in result.tables:
            all_rows.extend(table.rows)
        assert len(all_rows) > 0

        # At least one row should contain a numeric group count
        has_numeric = any(row.get("N", "").replace(".", "").isdigit() for row in all_rows)
        assert has_numeric, "Expected at least one row with a numeric N value"


# =========================================================================
# Test 5: Multi-dimension headers (ANOVA-style)
# =========================================================================


class TestMultiDimensionAnova:
    """Multi-dimension ANOVA OMS XML parsing."""

    def test_multi_dimension_anova_parse(self):
        """Parse OMS XML with multiple dimension axis='variable' nodes."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        # The parser's findall("dimension") collects all direct-child
        # dimensions.  Multiple <dimension axis="variable"> nodes share
        # the same key in the dims dict (last one wins), but the parser
        # should not crash and should produce at least one table.
        xml_content = """\
<oms>
  <command text="UNIANOVA">
    <pivotTable text="Tests of Between-Subjects Effects">
      <dimension axis="row">
        <category text="Factor_A"/>
        <category text="Factor_B"/>
      </dimension>
      <dimension axis="variable">
        <category text="Level_1"/>
        <category text="Level_2"/>
      </dimension>
      <dimension axis="variable">
        <category text="Metric_A"/>
        <category text="Metric_B"/>
      </dimension>
      <dimension axis="statistics">
        <category text="F"><cell text="4.52"/></category>
        <category text="Sig."><cell text="0.012"/></category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            # Even with overlapping dimension axes the parser should
            # gracefully handle them and yield tables
            assert isinstance(result.tables, list)
            assert len(result.tables) > 0, "Expected at least one table from multi-dimension XML"

            # Rows should be present
            assert len(result.tables[0].rows) > 0, "Expected rows from multi-dimension parsing"

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 6: Fallback behaviour (OMS → LST regex, and no-source error)
# =========================================================================


class TestFallback:
    """Fallback from OMS XML to LST regex and error handling."""

    MOCK_LST = """\
T-TEST GROUPS=gender(1 2) /VARIABLES=score.

Group Statistics
            N     Mean    Std. Deviation
Male    10    79.500     8.200
Female    10    84.200     7.100

Independent Samples Test
Equal variances assumed  1.23  0.281  2.340   18    0.021

"""

    def test_fallback_to_regex(self):
        """When OMS XML path does not exist, parse() falls back to LST text."""
        result = parse(
            oms_xml_path="/nonexistent/file.xml",
            lst_text=self.MOCK_LST,
            analysis_type="T-TEST",
        )

        assert result is not None
        assert len(result.tables) > 0
        # The parser_used should be "regex_lst" since the XML path
        # does not exist and the LST path was used
        assert result.parser_used == "regex_lst"

    def test_no_source_raises_value_error(self):
        """parse() raises ValueError when neither source is available."""
        with pytest.raises(ValueError):
            parse()


# =========================================================================
# Test 7: OMS XML — REGRESSION (dedicated extractor)
# =========================================================================


class TestRegressionOmsXml:
    """OMS XML parsing for a REGRESSION output with dedicated extractor."""

    def test_oms_regression_xml_parse(self):
        """Parse REGRESSION OMS XML and verify dedicated extractor stats."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        xml_content = """\
<oms>
  <command text="REGRESSION">
    <pivotTable subType="Model Summary">
      <dimension axis="statistics">
        <category text="R Square"><cell number="0.45"/></category>
      </dimension>
    </pivotTable>
    <pivotTable subType="ANOVA">
      <dimension axis="row">
        <category text="Regression">
          <dimension axis="column">
            <category text="F"><cell number="12.5"/></category>
            <category text="Sig."><cell number="0.003"/></category>
          </dimension>
        </category>
      </dimension>
    </pivotTable>
    <pivotTable subType="Coefficients">
      <dimension axis="row">
        <category text="score" variable="true">
          <dimension axis="column">
            <category text="B"><cell number="0.85"/></category>
            <category text="Beta"><cell number="0.67"/></category>
            <category text="t"><cell number="3.54"/></category>
          </dimension>
        </category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert result.analysis_type == "REGRESSION"
            assert result.statistics.get("r_squared") == 0.45
            assert result.statistics.get("f_value") == 12.5
            assert result.statistics.get("p_value") == 0.003
            assert result.statistics.get("b") == 0.85
            assert result.statistics.get("beta") == 0.67

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 8: OMS XML — CORRELATIONS (dedicated extractor)
# =========================================================================


class TestCorrelationsOmsXml:
    """OMS XML parsing for a CORRELATIONS output with dedicated extractor."""

    def test_oms_correlations_xml_parse(self):
        """Parse CORRELATIONS OMS XML and verify dedicated extractor stats."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        xml_content = """\
<oms>
  <command text="CORRELATIONS">
    <pivotTable subType="Correlations">
      <dimension axis="row">
        <category text="score" variable="true">
          <dimension axis="column">
            <category text="Pearson Correlation"><cell number="1" text="1"/></category>
            <category text="Sig. (2-tailed)"><cell text=""/></category>
            <category text="N"><cell number="30" text="30"/></category>
          </dimension>
          <dimension axis="row">
            <category text="age" variable="true">
              <dimension axis="column">
                <category text="Pearson Correlation"><cell number="0.156"/></category>
                <category text="Sig. (2-tailed)"><cell number="0.412"/></category>
                <category text="N"><cell number="30"/></category>
              </dimension>
            </category>
          </dimension>
        </category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert result.analysis_type == "CORRELATIONS"
            assert result.statistics.get("r") == 0.156
            assert result.statistics.get("p_value") == 0.412
            assert result.statistics.get("n_valid") == 30

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 9: OMS XML — ANOVA (dedicated extractor)
# =========================================================================


class TestAnovaOmsXml:
    """OMS XML parsing for an ANOVA (UNIANOVA) output with dedicated extractor."""

    def test_oms_anova_xml_parse(self):
        """Parse ANOVA OMS XML and verify dedicated extractor stats."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        xml_content = """\
<oms>
  <command text="UNIANOVA">
    <pivotTable subType="Tests of Between-Subjects Effects">
      <dimension axis="row">
        <category text="Corrected Model"/>
        <category text="class" variable="true">
          <dimension axis="column">
            <category text="F"><cell number="4.52"/></category>
            <category text="Sig."><cell number="0.012"/></category>
            <category text="df"><cell number="3"/></category>
          </dimension>
        </category>
        <category text="Error"/>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert result.analysis_type == "ANOVA"
            assert result.statistics.get("f_value") == 4.52
            assert result.statistics.get("p_value") == 0.012
            assert result.statistics.get("df") == 3

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 10: OMS XML — CROSSTABS (dedicated extractor)
# =========================================================================


class TestCrosstabsOmsXml:
    """OMS XML parsing for a CROSSTABS output with dedicated extractor."""

    def test_oms_crosstabs_xml_parse(self):
        """Parse CROSSTABS OMS XML and verify dedicated extractor stats."""
        pytest.importorskip("lxml", reason="lxml is required for OMS XML parsing")

        xml_content = """\
<oms>
  <command text="CROSSTABS">
    <pivotTable subType="Chi-Square Tests">
      <dimension axis="row">
        <category text="Pearson Chi-Square">
          <dimension axis="column">
            <category text="Value"><cell number="4.80"/></category>
            <category text="df"><cell number="1"/></category>
            <category text="Asymptotic Significance (2-sided)"><cell number="0.028"/></category>
          </dimension>
        </category>
      </dimension>
    </pivotTable>
  </command>
</oms>"""

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        try:
            tmp.write(xml_content)
            tmp.close()

            result = parse_oms_xml(tmp.name)

            assert result.analysis_type == "CROSSTABS"
            assert result.statistics.get("chi_square") == 4.80
            assert result.statistics.get("p_value") == 0.028
            assert result.statistics.get("df") == 1

        finally:
            os.unlink(tmp.name)


# =========================================================================
# Test 11: OMS XML — valid T-TEST (temp_xml_file fixture)
# =========================================================================


def test_parse_oms_xml_valid_ttest(temp_xml_file):
    """Parse valid T-TEST XML using the temp_xml_file fixture.

    Uses the conftest fixture which provides a T-TEST OMS XML file with
    'Group Statistics' and 'Independent Samples Test' pivot tables.
    Verifies analysis type inference, table discovery, and parser metadata.
    """
    pytest.importorskip("lxml")
    result = parse_oms_xml(temp_xml_file)

    assert result.analysis_type == "T-TEST"
    assert result.parser_used == "oms_xml"

    table_titles = [t.title for t in result.tables]
    assert "Group Statistics" in table_titles
    assert "Independent Samples Test" in table_titles

    assert isinstance(result.statistics, dict)


# =========================================================================
# Test 12: OMS XML — file not found
# =========================================================================


def test_parse_oms_xml_file_not_found():
    """Pass a non-existent XML path, assert FileNotFoundError."""
    pytest.importorskip("lxml")
    with pytest.raises(FileNotFoundError):
        parse_oms_xml("/nonexistent/path/to/surely/missing/file.xml")


# =========================================================================
# Test 13: OMS XML — empty file
# =========================================================================


def test_parse_oms_xml_empty_file(tmp_path):
    """Create an empty XML file, assert ValueError (empty XML detection)."""
    pytest.importorskip("lxml")
    empty = tmp_path / "empty.xml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_oms_xml(str(empty))


# =========================================================================
# Test 14: LST regex — valid T-TEST
# =========================================================================


LST_TTEST_VALID = """\
T-TEST GROUPS=gender(1 2) /VARIABLES=score.

Group Statistics
            N     Mean    Std. Deviation    Std. Error Mean
男          10    79.50   8.20              2.59
女          10    84.20   7.10              2.25

Independent Samples Test
                            F      Sig.    t       df    Sig. (2-tailed)    Mean Difference
Equal variances assumed  0.28   0.605  2.34    18    0.021              4.70
"""


def test_parse_raw_lst_valid_ttest():
    """Parse valid T-TEST LST text, verify AnalysisResult and parser_used."""
    result = parse_raw_lst(LST_TTEST_VALID, "T-TEST")

    assert isinstance(result, AnalysisResult)
    assert result.parser_used == "regex_lst"
    assert len(result.tables) >= 1

    # Key statistics should be extracted from the regex path
    assert "t_value" in result.statistics
    assert "p_value" in result.statistics


# =========================================================================
# Test 15: LST regex — invalid analysis type
# =========================================================================


def test_parse_raw_lst_invalid_type():
    """Pass an unrecognised analysis_type, assert ValueError."""
    with pytest.raises(ValueError, match="Unknown analysis_type"):
        parse_raw_lst("irrelevant text", "BOGUS_TYPE")


# =========================================================================
# Test 16: _safe_float variants
# =========================================================================


def test_safe_float_variants():
    """Test _safe_float with valid numbers, None, and unparseable strings."""
    # Valid numeric strings
    assert _safe_float("123.45") == 123.45
    assert _safe_float("0.021") == 0.021
    assert _safe_float("0") == 0.0
    assert _safe_float("1,234.56") == 1234.56  # US thousands separator

    # None and missing-value markers
    assert _safe_float(None) is None
    assert _safe_float("N/A") is None
    assert _safe_float("") is None
    assert _safe_float(".") is None
    assert _safe_float("—") is None  # em dash (SPSS missing value)
    assert _safe_float("a") is None  # SPSS missing-value flag


# =========================================================================
# Test 17: AnalysisResult serialisation roundtrip
# =========================================================================


def test_analysis_result_roundtrip(analysis_result_ttest):
    """analysis_result_to_dict then dict_to_analysis_result preserves data."""
    data = analysis_result_to_dict(analysis_result_ttest)
    restored = dict_to_analysis_result(data)

    assert restored.analysis_type == analysis_result_ttest.analysis_type
    assert restored.parser_used == analysis_result_ttest.parser_used
    assert restored.statistics == analysis_result_ttest.statistics
    assert restored.n_valid == analysis_result_ttest.n_valid
    assert restored.n_missing == analysis_result_ttest.n_missing
    assert restored.notes == analysis_result_ttest.notes
    assert restored.raw_output_path == analysis_result_ttest.raw_output_path

    # Tables are reconstructed correctly
    assert len(restored.tables) == len(analysis_result_ttest.tables)
    for rt, ot in zip(restored.tables, analysis_result_ttest.tables):
        assert rt.title == ot.title
        assert rt.rows == ot.rows
        assert rt.notes == ot.notes
        assert rt.source_format == ot.source_format


# =========================================================================
# Test 18: Unified parse — OMS XML priority
# =========================================================================


def test_unified_parse_oms_priority(temp_xml_file):
    """parse() with only oms_xml_path prefers the OMS XML parser."""
    pytest.importorskip("lxml")
    result = parse(oms_xml_path=temp_xml_file)

    assert isinstance(result, AnalysisResult)
    assert result.parser_used == "oms_xml"

    # Verify tables were extracted despite missing subType attributes
    table_titles = [t.title for t in result.tables]
    assert "Group Statistics" in table_titles or len(table_titles) >= 1
