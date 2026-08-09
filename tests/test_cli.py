import tempfile
from pathlib import Path

import pytest 
import depvex.resolver as resolver_module # ignore depvex
from depvex.cli import DepvexCLI  # ignore depvex
from depvex.parser import ImportExtractor  # ignore depvex
from depvex.resolver import DependencyResolver  # ignore depvex


def test_scan_updates_requirements_for_a_single_run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text("import flet\n", encoding="utf-8")

        cli = DepvexCLI()
        exit_code = cli.scan(tmpdir)

        assert exit_code == 0
        requirements_path = Path(tmpdir) / "requirements.txt"
        assert requirements_path.exists()
        assert any(line.startswith("flet") for line in requirements_path.read_text(encoding="utf-8").splitlines())


def test_check_returns_non_zero_when_requirements_are_outdated() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text("import requests\n", encoding="utf-8")

        requirements_path = Path(tmpdir) / "requirements.txt"
        requirements_path.write_text("old-package==1.0\n", encoding="utf-8")

        cli = DepvexCLI()
        exit_code = cli.check(tmpdir)

        assert exit_code != 0


def test_imports_marked_with_ignore_comment_are_skipped() -> None:
    extractor = ImportExtractor()
    code = "import requests  # ignore depvex\nimport flet\n"

    assert extractor.extract_imports(code) == ["flet"]


def test_ignore_packages_excludes_a_dependency_from_requirements() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text("import requests\n", encoding="utf-8")

        resolver = DependencyResolver(root=tmpdir)
        resolver.IGNORE_PACKAGES = {"requests"}

        assert resolver.requirements_for(tmpdir) == []


def test_resolver_uses_import_mapping_when_module_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text("import yaml\n", encoding="utf-8")

        resolver = DependencyResolver(root=tmpdir)
        resolver.IMPORT_MAPPING = {"yaml": ["pyyaml"]}
        monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
        monkeypatch.setattr(DependencyResolver, "is_installed", lambda self, module_name: False)
        monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

        requirements = resolver.requirements_for(tmpdir)

        assert requirements == ["pyyaml"]


def test_resolver_prefers_import_mapping_even_when_module_is_already_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = DependencyResolver(root=".")
    resolver.IMPORT_MAPPING = {"dbt": ["dbt-core"]}
    resolver.top_level_distributions = {}
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "is_installed", lambda self, module_name: True)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    assert resolver.resolve("dbt", has_net=False) == "dbt-core"


def test_resolver_loads_mapping_from_package_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_dir = tmp_path / "depvex"
    package_dir.mkdir()
    (package_dir / "import_mapping_filtered.txt").write_text("yaml:pyyaml\n", encoding="utf-8")
    module_path = package_dir / "resolver.py"
    module_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(resolver_module, "__file__", str(module_path))
    monkeypatch.setattr(resolver_module, "IMPORT_MAPPING_FILE", tmp_path / "missing.txt")

    resolver = resolver_module.DependencyResolver(root=str(tmp_path))

    assert resolver.IMPORT_MAPPING["yaml"] == ["pyyaml"]


def test_check_prints_stale_dependencies(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text("import requests\n", encoding="utf-8")
        (Path(tmpdir) / "requirements.txt").write_text("old-package==1.0\n", encoding="utf-8")
        monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)

        assert DepvexCLI().check(tmpdir) == 1
        assert "stale: old-package==1.0" in capsys.readouterr().out


def test_pyproject_dependencies_can_be_written_and_read() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text('[project]\nname = "sample"\ndependencies = ["old-package==1.0"]\n', encoding="utf-8")

        resolver = DependencyResolver(root=tmpdir)
        resolver.write_pyproject_dependencies(str(pyproject_path), ["requests==2.34.2"])

        assert resolver.read_pyproject_dependencies(str(pyproject_path)) == ["requests==2.34.2"]


def test_report_and_pyproject_flags_are_available() -> None:
    args = DepvexCLI().parser.parse_args(["--report", "--pyproject", "."])

    assert args.command == "report"
    assert args.pyproject is True
