import json
import tempfile
from pathlib import Path

import pytest

import depvex.resolver as resolver_module
from depvex.cli import DepvexCLI
from depvex.parser import ImportExtractor
from depvex.resolver import DependencyResolver


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


def test_literal_dynamic_import_is_discovered() -> None:
    extractor = ImportExtractor()

    assert extractor.extract_imports('importlib.import_module("requests.sessions")') == ["requests"]


def test_computed_dynamic_import_creates_actionable_warning(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "loader.py").write_text("importlib.import_module(plugin_name)\n", encoding="utf-8")

        resolver = DependencyResolver(root=tmpdir)
        assert resolver.discover_imports(tmpdir) == set()
        resolver.print_dynamic_import_warnings()

        assert "dynamic_imports" in capsys.readouterr().out


def test_yaml_dynamic_imports_are_added_to_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "depvex.yaml").write_text("dynamic_imports:\n  - celery\n", encoding="utf-8")
    resolver = DependencyResolver(root=str(tmp_path))
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    assert resolver.requirements_for(str(tmp_path)) == ["celery"]


def test_test_only_imports_are_written_to_dev_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("import pytest\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "sample"\ndependencies = []\n', encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    assert DepvexCLI().scan(str(tmp_path), use_pyproject=True) == 0
    resolver = DependencyResolver(root=str(tmp_path))
    assert resolver.read_pyproject_dependencies(str(tmp_path / "pyproject.toml")) == ["requests"]
    assert resolver.read_pyproject_optional_dependencies(str(tmp_path / "pyproject.toml")) == ["pytest"]


def test_diff_does_not_write_requirements(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)

    assert DepvexCLI().diff(str(tmp_path)) == 1
    assert not (tmp_path / "requirements.txt").exists()


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


def test_type_checking_imports_are_skipped() -> None:
    extractor = ImportExtractor()
    code = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import pandas\n"
        "    from sklearn import metrics\n"
        "import requests\n"
    )
    assert extractor.extract_imports(code) == ["requests"]


def test_typing_attribute_type_checking_is_skipped() -> None:
    extractor = ImportExtractor()
    code = "import typing\n" "if typing.TYPE_CHECKING:\n" "    import torch\n" "import flet\n"
    assert extractor.extract_imports(code) == ["flet"]


def test_aliased_typing_type_checking_is_skipped() -> None:
    extractor = ImportExtractor()
    code = "import typing as t\n" "if t.TYPE_CHECKING:\n" "    import scipy\n" "import click\n"
    assert extractor.extract_imports(code) == ["click"]


def test_not_type_checking_orelse_is_skipped() -> None:
    extractor = ImportExtractor()
    code = (
        "from typing import TYPE_CHECKING\n"
        "if not TYPE_CHECKING:\n"
        "    import requests\n"
        "else:\n"
        "    import polars\n"
    )
    assert extractor.extract_imports(code) == ["requests"]


def test_runtime_import_outside_type_checking_is_retained() -> None:
    extractor = ImportExtractor()
    code = (
        "from typing import TYPE_CHECKING\n"
        "import requests\n"
        "if TYPE_CHECKING:\n"
        "    import requests\n"
        "    import pandas\n"
    )
    assert extractor.extract_imports(code) == ["requests"]


def test_dynamic_imports_in_type_checking_are_skipped() -> None:
    extractor = ImportExtractor()
    code = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import importlib\n"
        "    importlib.import_module('celery')\n"
        "    importlib.import_module(dynamic_var)\n"
    )
    assert extractor.extract_imports(code) == []
    assert extractor.extract_dynamic_imports(code) == []


def test_scan_does_not_include_type_checking_imports_in_requirements() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_file = Path(tmpdir) / "sample.py"
        sample_file.write_text(
            "from typing import TYPE_CHECKING\n" "if TYPE_CHECKING:\n" "    import pandas\n" "import flet\n",
            encoding="utf-8",
        )

        cli = DepvexCLI()
        exit_code = cli.scan(tmpdir)

        assert exit_code == 0
        requirements_path = Path(tmpdir) / "requirements.txt"
        assert requirements_path.exists()
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
        assert any(line.startswith("flet") for line in lines)
        assert not any(line.startswith("pandas") for line in lines)


def test_extract_notebook_code_and_imports_with_magics() -> None:
    extractor = ImportExtractor()
    notebook_json = json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "source": ["# Title\n", "Some text"]},
                {
                    "cell_type": "code",
                    "source": ["%matplotlib inline\n", "!pip install whatever\n", "import seaborn as sns\n"],
                },
                {"cell_type": "code", "source": "df.info?\nfrom sklearn.cluster import KMeans\n"},
            ]
        }
    )
    imports = extractor.extract_notebook_imports(notebook_json)
    assert sorted(imports) == ["seaborn", "sklearn"]


def test_scan_creates_requirements_notebooks_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    notebook_content = json.dumps({"cells": [{"cell_type": "code", "source": ["import matplotlib\n"]}]})
    (tmp_path / "analysis.ipynb").write_text(notebook_content, encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    exit_code = DepvexCLI().scan(str(tmp_path))
    assert exit_code == 0

    req_path = tmp_path / "requirements.txt"
    nb_req_path = tmp_path / "requirements-notebooks.txt"

    assert req_path.exists()
    assert nb_req_path.exists()

    req_lines = req_path.read_text(encoding="utf-8").splitlines()
    nb_lines = nb_req_path.read_text(encoding="utf-8").splitlines()

    assert "requests" in req_lines
    assert "matplotlib" not in req_lines
    assert "matplotlib" in nb_lines
    assert "requests" not in nb_lines


def test_scan_merges_notebooks_when_target_is_requirements_txt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "depvex.yaml").write_text("notebooks_target: 'requirements.txt'\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    notebook_content = json.dumps({"cells": [{"cell_type": "code", "source": ["import matplotlib\n"]}]})
    (tmp_path / "analysis.ipynb").write_text(notebook_content, encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    exit_code = DepvexCLI().scan(str(tmp_path))
    assert exit_code == 0

    req_path = tmp_path / "requirements.txt"
    nb_req_path = tmp_path / "requirements-notebooks.txt"

    assert req_path.exists()
    assert not nb_req_path.exists()

    req_lines = req_path.read_text(encoding="utf-8").splitlines()
    assert "requests" in req_lines
    assert "matplotlib" in req_lines


def test_scan_custom_notebooks_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "depvex.yaml").write_text("notebooks_target: 'custom-notebooks.txt'\n", encoding="utf-8")
    notebook_content = json.dumps({"cells": [{"cell_type": "code", "source": ["import matplotlib\n"]}]})
    (tmp_path / "analysis.ipynb").write_text(notebook_content, encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    exit_code = DepvexCLI().scan(str(tmp_path))
    assert exit_code == 0

    assert (tmp_path / "custom-notebooks.txt").exists()
    assert not (tmp_path / "requirements-notebooks.txt").exists()
    lines = (tmp_path / "custom-notebooks.txt").read_text(encoding="utf-8").splitlines()
    assert "matplotlib" in lines


def test_check_fails_when_requirements_notebooks_is_missing_or_outdated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notebook_content = json.dumps({"cells": [{"cell_type": "code", "source": ["import matplotlib\n"]}]})
    (tmp_path / "analysis.ipynb").write_text(notebook_content, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)

    # Missing requirements-notebooks.txt -> fails
    assert DepvexCLI().check(str(tmp_path)) != 0

    # Outdated requirements-notebooks.txt -> fails
    (tmp_path / "requirements-notebooks.txt").write_text("old-package==1.0\n", encoding="utf-8")
    assert DepvexCLI().check(str(tmp_path)) != 0

    # Up to date requirements-notebooks.txt -> passes
    (tmp_path / "requirements-notebooks.txt").write_text("matplotlib\n", encoding="utf-8")
    assert DepvexCLI().check(str(tmp_path)) == 0


def test_diff_detects_notebook_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    notebook_content = json.dumps({"cells": [{"cell_type": "code", "source": ["import matplotlib\n"]}]})
    (tmp_path / "analysis.ipynb").write_text(notebook_content, encoding="utf-8")
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)

    assert DepvexCLI().diff(str(tmp_path)) == 1
    assert not (tmp_path / "requirements-notebooks.txt").exists()


def test_version_flag_prints_version_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    cli = DepvexCLI()
    with pytest.raises(SystemExit) as exc_info:
        cli.run(["--version"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "depvex" in captured.out
