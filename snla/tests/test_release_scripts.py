import threading
import time

from scripts.compare_backends import (
    PROJECT_ROOT,
    _build_comparison_json,
    _build_trust_json,
    _compare_method_stats,
    _compute_method_trust,
    _missing_required_stats,
    _parse_filter,
    _release_cases,
    _release_gate_failed,
    _run_with_timeout,
)
from scripts.e2e_backend_smoke import SMOKE_CASES, validate_outcome
from snla.capabilities import get_public_capabilities
from snla.executor.spss import _oms_output_complete


def test_backend_comparison_all_filter_selects_every_method():
    assert _parse_filter(None) is None
    assert _parse_filter("all") is None
    assert _parse_filter(" ALL ") is None


def test_backend_comparison_filter_trims_comma_separated_values():
    assert _parse_filter("descriptives, independent_t_test") == [
        "descriptives",
        "independent_t_test",
    ]


def test_release_gate_excludes_diagnostics_unless_explicitly_requested():
    cases = [{"id": "release"}, {"id": "batch", "diagnostic_only": True}]

    assert _release_cases(cases, include_diagnostics=False, ids_filter=None) == [cases[0]]
    assert _release_cases(cases, include_diagnostics=True, ids_filter=None) == cases
    assert _release_cases(cases, include_diagnostics=False, ids_filter=["batch"]) == cases


def test_release_gate_fails_for_requested_backend_failure_or_conflict():
    success = {
        "spss_success": True,
        "py_success": True,
        "conclusion_conflict": False,
        "comparison_passed": True,
    }
    assert _release_gate_failed([success], None) is False
    assert _release_gate_failed([{**success, "spss_success": False}], None) is True
    assert _release_gate_failed([{**success, "py_success": False}], "python") is True
    assert _release_gate_failed([{**success, "conclusion_conflict": True}], None) is True
    assert _release_gate_failed([{**success, "comparison_passed": False}], None) is True


def test_all_11_capabilities_define_justified_comparison_tolerances():
    capabilities = get_public_capabilities()

    assert len(capabilities) == 11
    assert len({capability.name for capability in capabilities}) == 11
    for capability in capabilities:
        assert capability.comparison_tolerances
        for tolerance in capability.comparison_tolerances:
            assert tolerance.metric
            assert tolerance.absolute >= 0
            assert tolerance.relative >= 0
            assert tolerance.rationale.endswith(".")


def test_method_comparison_requires_schema_and_numeric_tolerance():
    exact = {"r": 0.5, "p_value": 0.02, "n_valid": 30}
    passed = _compare_method_stats("pearson_correlation", exact, exact, 0.05)
    missing = _compare_method_stats(
        "pearson_correlation",
        exact,
        {"p_value": 0.02, "n_valid": 30},
        0.05,
    )
    outside = _compare_method_stats(
        "pearson_correlation",
        exact,
        {"r": 0.7, "p_value": 0.02, "n_valid": 30},
        0.05,
    )

    assert passed["comparison_passed"] is True
    assert missing["schema_complete"] is False
    assert missing["missing_fields"] == ["r"]
    assert outside["schema_complete"] is True
    assert outside["numeric_match"] is False


def test_method_trust_folds_aliases_and_never_ignores_failures():
    base = {
        "spss_success": True,
        "py_success": True,
        "conclusion_conflict": False,
        "schema_complete": True,
        "numeric_match": True,
    }
    results = [
        {**base, "method": "pearson_correlation"},
        {**base, "method": "correlations"},
        {**base, "method": "descriptives", "py_success": False},
    ]

    trust = _compute_method_trust(results, 0.05, 0.98)

    assert len(trust) == 11
    assert "correlations" not in trust
    assert trust["pearson_correlation"]["cases_total"] == 2
    assert trust["pearson_correlation"]["trusted"] is True
    assert trust["descriptives"]["trusted"] is False
    assert trust["descriptives"]["cases_failed"] == 1


def test_release_json_artifacts_are_evidence_only_and_cover_11_methods():
    comparison = _build_comparison_json([], 0.05, False, True)
    trust = _build_trust_json(
        _compute_method_trust([], 0.05, 0.98),
        0.05,
        0.98,
    )

    assert comparison["meta"]["canonical_methods"] == 11
    assert len(comparison["meta"]["comparison_schemas"]) == 11
    assert trust["evidence_only"] is True
    assert trust["runtime_trust_source"] == "capability_registry"
    assert trust["canonical_methods"] == 11
    assert len(trust["methods"]) == 11


def test_inferential_methods_require_a_p_value_but_descriptives_do_not():
    assert _missing_required_stats("oneway_anova", {"f_value": 1.2}) is True
    assert _missing_required_stats("oneway_anova", {"p_value": 0.2}) is False
    assert _missing_required_stats("descriptives", {"mean": 3.0}) is False


def test_timeout_invokes_termination_callback_without_waiting_for_worker():
    release_worker = threading.Event()
    terminated = threading.Event()

    def slow_operation():
        release_worker.wait(2)

    def terminate():
        terminated.set()
        release_worker.set()

    started = time.perf_counter()
    result, error = _run_with_timeout(slow_operation, timeout=0.01, on_timeout=terminate)

    assert result is None
    assert error == "Timed out after 0.01s"
    assert terminated.is_set()
    assert time.perf_counter() - started < 1


def test_oms_completion_requires_the_document_closing_tag(tmp_path):
    output = tmp_path / "result.xml"
    output.write_text("<outputTree><command/></outputTree>\n", encoding="utf-8")
    assert _oms_output_complete(str(output)) is True

    output.write_text("<outputTree><command>", encoding="utf-8")
    assert _oms_output_complete(str(output)) is False
    assert _oms_output_complete(str(tmp_path / "missing.xml")) is False


def test_smoke_cases_pin_method_and_variable_roles():
    assert len(SMOKE_CASES) == 5
    assert all(case["expected_method"] for case in SMOKE_CASES)
    assert all("test_variable" in case for case in SMOKE_CASES)
    public_methods = {capability.name for capability in get_public_capabilities()}
    assert {case["expected_method"] for case in SMOKE_CASES} <= public_methods


def test_smoke_outcome_rejects_a_successful_response_with_the_wrong_method():
    valid, message = validate_outcome(
        {"ok": True, "method": "independent_t_test"},
        {"expected_method": "pearson_correlation"},
    )

    assert valid is False
    assert "expected pearson_correlation" in message


def test_package_spec_tracks_analysis_service_without_removed_pipeline():
    spec = (PROJECT_ROOT / "snla.spec").read_text(encoding="utf-8")

    assert "snla.analysis.service" in spec
    assert "snla.analysis.applicability" in spec
    assert "snla.ui._pipeline" not in spec


def test_spss_release_report_has_required_signoff_fields_and_no_account_judgment():
    report = (PROJECT_ROOT / "docs" / "release" / "RELEASE_SPSS_VALIDATION.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "Validation date",
        "Reviewer",
        "Windows version",
        "SPSS version: 26",
        "Matrix summary",
        "OMS parsing outcome",
        "Reviewer signature",
        "Release Decision",
    ):
        assert required in report
    for forbidden in ("license", "licence", "正版", "授权", "许可证"):
        assert forbidden.casefold() not in report.casefold()


def test_inno_installer_is_per_user_and_cleanup_is_explicitly_consented():
    installer = (PROJECT_ROOT / "packaging" / "StatsTalk.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in installer
    assert "PrivilegesRequiredOverridesAllowed" not in installer
    assert "{localappdata}\\Programs\\StatsTalk" in installer
    assert "InitializeUninstall" in installer
    assert "RemoveUserData := MsgBox" in installer
    assert "DelTree(ExpandConstant('{userappdata}\\StatsTalk')" in installer
    assert (
        "Your original datasets and exported API-key backup files will not be deleted" in installer
    )
    assert "WebView2Installed" in installer
    assert "token-protected local interface" in installer


def test_portable_package_documents_isolated_data_and_dpapi_migration():
    readme = (PROJECT_ROOT / "packaging" / "PORTABLE_README.txt").read_text(encoding="utf-8")

    assert "portable.marker" in readme
    assert "Data folder next to StatsTalk.exe" in readme
    assert "DPAPI-protected key remains bound to the Windows account" in readme
    assert "export a password-protected key backup" in readme
    assert "token-protected local interface" in readme


def test_release_builder_generates_versioned_artifacts_and_sha256_manifest():
    script = (PROJECT_ROOT / "scripts" / "build_windows_release.ps1").read_text(encoding="utf-8")

    assert "from snla.version import APP_VERSION" in script
    assert "PyInstaller snla.spec" in script
    assert "StatsTalk-$version-windows-x64-portable" in script
    assert "StatsTalk-$version-windows-x64-setup.exe" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "SHA256SUMS.txt" in script
    assert "release-manifest.json" in script
