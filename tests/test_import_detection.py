"""Import-detection regressions: what counts as a third-party dependency, and which one."""

from pathlib import Path

import pytest

from depvex.cli import DepvexCLI
from depvex.parser import ImportExtractor
from depvex.resolver import DependencyResolver, canonical_package_name, requirement_name


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test independent of the network and of what is installed here."""
    monkeypatch.setattr(DependencyResolver, "internet_check", lambda self: False)
    monkeypatch.setattr(DependencyResolver, "get_local_version", lambda self, package_name: None)


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def requirements(root: Path, **kwargs: object) -> list[str]:
    return DependencyResolver(root=str(root)).requirements_for(str(root), **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- parser


def test_relative_imports_are_never_third_party() -> None:
    code = "from . import sibling\nfrom .helpers import thing\nfrom ..pkg.mod import other\nimport requests\n"

    assert ImportExtractor().extract_imports(code) == ["requests"]


def test_relative_dynamic_import_is_ignored() -> None:
    extractor = ImportExtractor()

    assert extractor.extract_imports('import importlib\nimportlib.import_module(".plugin")\n') == []


def test_stdlib_dynamic_import_is_not_a_dependency() -> None:
    assert ImportExtractor().extract_imports('importlib.import_module("os.path")\n') == []


def test_source_with_non_utf8_encoding_cookie_is_parsed() -> None:
    source = b'# -*- coding: latin-1 -*-\nimport yaml\nname = "caf\xe9"\n'

    result = ImportExtractor().parse_source(source)

    assert [record.module for record in result.records] == ["yaml"]
    assert result.used_fallback is False


def test_python2_source_falls_back_to_line_scanning() -> None:
    source = b'import numpy\nprint "legacy"\nfrom six.moves import urllib; import scipy.sparse, os\n'

    result = ImportExtractor().parse_source(source)

    assert result.used_fallback is True
    assert result.error
    assert {record.top_level for record in result.records} == {"numpy", "six", "scipy"}


def test_fallback_ignores_imports_in_docstrings_and_comments() -> None:
    source = 'print "py2"\n"""\nimport inside_docstring\n"""\n# import commented\nimport real\nimport skipped  # ignore depvex\n'

    result = ImportExtractor().parse_source(source)

    assert [record.module for record in result.records] == ["real"]


def test_try_except_import_error_marks_imports_optional() -> None:
    code = (
        "import requests\n"
        "try:\n"
        "    import ujson as json\n"
        "except ImportError:\n"
        "    import simplejson as json\n"
        "try:\n"
        "    import orjson\n"
        "except (ValueError, ModuleNotFoundError):\n"
        "    orjson = None\n"
    )

    records = {record.module: record.optional for record in ImportExtractor().extract_records(code)}

    assert records == {"requests": False, "ujson": True, "simplejson": True, "orjson": True}


def test_try_without_import_error_handler_is_not_optional() -> None:
    code = "try:\n    import requests\nexcept ValueError:\n    pass\n"

    assert [r.optional for r in ImportExtractor().extract_records(code)] == [False]


def test_pytest_plugins_and_installed_apps_are_discovered() -> None:
    code = (
        'pytest_plugins = ["pytest_asyncio", "myproject.fixtures"]\n'
        'INSTALLED_APPS = ["django.contrib.admin", "rest_framework", "not a module"]\n'
    )

    imports = ImportExtractor().extract_imports(code)

    assert imports == ["django", "myproject", "pytest_asyncio", "rest_framework"]


def test_ignore_comment_applies_to_multiline_imports() -> None:
    code = "from requests import (\n    get,\n    post,  # ignore depvex\n)\nimport flet\n"

    assert ImportExtractor().extract_imports(code) == ["flet"]


# --------------------------------------------------------------------- local modules


def test_src_layout_package_is_local_not_a_dependency(tmp_path: Path) -> None:
    write(tmp_path / "src" / "mypkg" / "__init__.py")
    write(tmp_path / "src" / "mypkg" / "core.py", "import requests\n")
    write(tmp_path / "app.py", "import mypkg\nfrom mypkg.core import x\n")

    assert requirements(tmp_path) == ["requests"]


def test_sibling_module_in_a_script_directory_is_local(tmp_path: Path) -> None:
    write(tmp_path / "scripts" / "run.py", "import helper_mod\nimport requests\n")
    write(tmp_path / "scripts" / "helper_mod.py")

    assert requirements(tmp_path) == ["requests"]


def test_relative_import_of_a_missing_name_is_not_resolved_from_pypi(tmp_path: Path) -> None:
    write(tmp_path / "pkg" / "__init__.py")
    write(tmp_path / "pkg" / "core.py", "from .helpers import thing\nfrom . import sibling\n")

    assert requirements(tmp_path) == []


def test_module_local_to_one_directory_does_not_hide_the_dependency_elsewhere(tmp_path: Path) -> None:
    write(tmp_path / "vendor" / "requests" / "__init__.py")
    write(tmp_path / "app.py", "import requests\n")

    assert requirements(tmp_path) == ["requests"]


def test_data_directory_without_python_does_not_hide_a_dependency(tmp_path: Path) -> None:
    (tmp_path / "redis").mkdir()
    write(tmp_path / "redis" / "redis.conf", "port 6379\n")
    write(tmp_path / "app.py", "import redis\n")

    assert requirements(tmp_path) == ["redis"]


# ---------------------------------------------------------------- namespace packages


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("from google.cloud import storage\n", ["google-cloud-storage"]),
        ("from google.cloud import storage, bigquery\n", ["google-cloud-bigquery", "google-cloud-storage"]),
        ("import google.cloud.storage\n", ["google-cloud-storage"]),
        ("from google.cloud.storage import Client\n", ["google-cloud-storage"]),
        ("import azure.storage.blob.aio\n", ["azure-storage-blob"]),
        ("from azure.identity import DefaultAzureCredential\n", ["azure-identity"]),
        ("from azure.mgmt.compute import ComputeManagementClient\n", ["azure-mgmt-compute"]),
        ("from google.protobuf import message\n", ["protobuf"]),
        ("import google.auth\n", ["google-auth"]),
        ("from google import genai\n", ["google-genai"]),
        ("import opentelemetry.sdk.trace\n", ["opentelemetry-sdk"]),
        ("from opentelemetry import trace\n", ["opentelemetry-api"]),
        ("import ruamel.yaml\n", ["ruamel-yaml"]),
    ],
)
def test_namespace_imports_resolve_to_the_right_distribution(tmp_path: Path, code: str, expected: list[str]) -> None:
    write(tmp_path / "app.py", code)

    assert sorted(requirements(tmp_path)) == expected


def test_regular_package_attributes_are_not_mistaken_for_namespaces(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "from flask import cli, json\nfrom yaml import safe_load\nimport requests.adapters\n")

    # Distribution spelling can vary with what is installed (Flask vs flask); the set of packages cannot.
    assert sorted(entry.lower() for entry in requirements(tmp_path)) == ["flask", "pyyaml", "requests"]


# ------------------------------------------------------------------ optional imports


def test_optional_imports_are_included_by_default_and_reported(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "try:\n    import ujson\nexcept ImportError:\n    ujson = None\nimport requests\n")
    resolver = DependencyResolver(root=str(tmp_path))

    assert sorted(resolver.requirements_for(str(tmp_path))) == ["requests", "ujson"]
    assert resolver.optional_imports == {"ujson"}


def test_optional_imports_can_be_excluded_from_requirements(tmp_path: Path) -> None:
    write(tmp_path / "depvex.yaml", "optional_imports: exclude\n")
    write(tmp_path / "app.py", "try:\n    import ujson\nexcept ImportError:\n    ujson = None\nimport requests\n")
    write(tmp_path / "other.py", "import orjson\n")
    write(tmp_path / "third.py", "try:\n    import orjson\nexcept ImportError:\n    pass\n")

    assert sorted(requirements(tmp_path)) == ["orjson", "requests"]


# --------------------------------------------------------- unparsable / encoded files


def test_scan_recovers_imports_from_python2_and_latin1_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "legacy.py").write_bytes(b'import numpy\nprint "old"\n')
    (tmp_path / "latin.py").write_bytes(b'# -*- coding: latin-1 -*-\nimport yaml\nx = "\xe9"\n')

    assert DepvexCLI().scan(str(tmp_path)) == 0

    assert sorted((tmp_path / "requirements.txt").read_text(encoding="utf-8").split()) == ["numpy", "pyyaml"]
    output = capsys.readouterr().out
    assert "legacy.py could not be parsed" in output
    assert "latin.py" not in output


# --------------------------------------------------------------- requirements files


def test_requirements_header_options_and_comments_survive_a_scan(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "import requests\nimport yaml\n")
    write(
        tmp_path / "requirements.txt",
        "# Managed by hand\n"
        "\n"
        "--index-url https://example.com/simple\n"
        "-r base.txt\n"
        "# keep this pinned\n"
        "requests==2.0  # security fix\n"
        "old-package==1.0\n",
    )

    assert DepvexCLI().scan(str(tmp_path)) == 0

    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8").splitlines() == [
        "# Managed by hand",
        "",
        "--index-url https://example.com/simple",
        "-r base.txt",
        "pyyaml",
        "# keep this pinned",
        "requests==2.0  # security fix",
    ]


def test_scanning_twice_gives_the_same_requirements_file(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "import requests\n")
    write(tmp_path / "requirements.txt", "# header\n\n-r base.txt\nrequests==2.0\n")

    DepvexCLI().scan(str(tmp_path))
    first = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    DepvexCLI().scan(str(tmp_path))

    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == first


def test_check_does_not_report_option_lines_as_stale(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "import requests\n")
    write(tmp_path / "requirements.txt", "--index-url https://example.com/simple\n-e .\nrequests==2.0\n")

    assert DepvexCLI().check(str(tmp_path)) == 0


def test_equivalent_spellings_of_a_package_name_are_one_requirement(tmp_path: Path) -> None:
    write(tmp_path / "app.py", "import dateutil\n")
    write(tmp_path / "requirements.txt", "python_dateutil==2.9.0\n")

    assert requirements(tmp_path) == ["python_dateutil==2.9.0"]
    assert DepvexCLI().check(str(tmp_path)) == 0


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("requests==2.0", "requests"),
        ("Python_Dateutil>=2.8 ; python_version>'3.6'", "python-dateutil"),
        ("uvicorn[standard]>=0.30", "uvicorn"),
        ("pkg @ https://example.com/pkg.whl", "pkg"),
        ("--index-url https://example.com", ""),
        ("-e .", ""),
        ("git+https://github.com/org/repo.git#egg=repo", ""),
        ("# comment", ""),
    ],
)
def test_requirement_name_parsing(entry: str, expected: str) -> None:
    assert requirement_name(entry) == expected


def test_canonical_package_name_follows_pep_503() -> None:
    assert canonical_package_name("Ruamel.YAML_clib") == "ruamel-yaml-clib"


# ------------------------------------------------------------------------- logging


def test_scan_is_quiet_unless_verbose(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path / "app.py", "import requests\n")

    DepvexCLI().scan(str(tmp_path))

    assert "[debug]" not in capsys.readouterr().out


def test_verbose_flag_is_available() -> None:
    assert DepvexCLI().parser.parse_args(["--scan", "--verbose", "."]).verbose is True


def test_scanning_inside_a_package_directory_treats_the_package_as_local(tmp_path: Path) -> None:
    package = tmp_path / "mypackage"
    write(package / "__init__.py")
    write(package / "core.py", "import mypackage.util\nimport requests\n")
    write(package / "util.py")

    assert requirements(package) == ["requests"]
