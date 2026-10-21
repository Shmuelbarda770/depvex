import json

from depvex.compatibility import CompatibilityAnalyzer, report_as_json


def test_compatibility_detects_transitive_conflict(monkeypatch, tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("urllib3==1.26.0\n", encoding="utf-8")
    metadata = {
        "newlib": {"version": "1.0", "requires_dist": ["urllib3>=2.0"]},
        "urllib3": {
            "version": "1.26.0",
            "requires_dist": [],
            "releases": {"1.26.0": {}, "2.0.0": {}},
        },
    }
    analyzer = CompatibilityAnalyzer(root=str(tmp_path), allow_network=False)
    monkeypatch.setattr(analyzer, "_metadata", lambda package: metadata.get(package.lower()))

    report = analyzer.analyze("newlib")

    assert report.compatible is False
    assert any(issue.code == "transitive-conflict" for issue in report.issues)


def test_compatibility_reports_unknown_target_offline(tmp_path) -> None:
    report = CompatibilityAnalyzer(root=str(tmp_path), allow_network=False).analyze("missing-library")

    assert report.compatible is True
    assert report.uncertain is True
    assert report.issues[0].code == "candidate-unknown"


def test_compatibility_report_is_json_serializable(tmp_path) -> None:
    report = CompatibilityAnalyzer(root=str(tmp_path), allow_network=False).analyze("missing-library")

    payload = json.loads(report_as_json(report))

    assert payload["target"] == "missing-library"
    assert payload["issues"][0]["severity"] == "warning"


def test_unknown_transitive_metadata_makes_result_uncertain(monkeypatch, tmp_path) -> None:
    metadata = {"newlib": {"version": "1.0", "requires_dist": ["missing-dependency>=1"]}}
    analyzer = CompatibilityAnalyzer(root=str(tmp_path), allow_network=False)
    monkeypatch.setattr(analyzer, "_metadata", lambda package: metadata.get(package.lower()))

    report = analyzer.analyze("newlib")

    assert report.compatible is True
    assert report.uncertain is True
    assert any(issue.code == "dependency-metadata-unknown" for issue in report.issues)


def test_inactive_requirement_marker_is_not_considered(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "urllib3==1.26.0; python_version < '3.0'\n", encoding="utf-8"
    )
    analyzer = CompatibilityAnalyzer(root=str(tmp_path), allow_network=False)
    analyzer._metadata_cache["newlib"] = {"version": "1.0", "requires_dist": ["urllib3>=2"]}

    report = analyzer.analyze("newlib")

    assert report.compatible is True
    assert not any(issue.code == "transitive-conflict" for issue in report.issues)