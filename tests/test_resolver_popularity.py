import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from depvex.resolver import DependencyResolver


@pytest.fixture
def temp_project_dir() -> Iterator[tuple[Path, Path, Path]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        import_mapping = tmp_path / "import_mapping_filtered.txt"
        import_mapping.write_text(
            "aioboto3_stubs: types-aioboto3\n"
            "aioboto3_stubs: types-aioboto3-lite\n"
            "ai_core_sdk: sap-ai-sdk-core\n"
            "ai_core_sdk: ai-core-sdk\n",
            encoding="utf-8",
        )

        popularity_file = tmp_path / "duplicate_imports_pop.txt"
        popularity_file.write_text(
            "ai_core_sdk:\n"
            "    sap-ai-sdk-core | downloads: 200,864\n"
            "    ai-core-sdk | downloads: 179,836\n"
            "\n"
            "aioboto3-stubs:\n"
            "    types-aioboto3 | downloads: 6,048,527\n"
            "    types-aioboto3-lite | downloads: 73,411\n",
            encoding="utf-8",
        )

        yield tmp_path, import_mapping, popularity_file


def test_popularity_file_parsing(temp_project_dir: tuple[Path, Path, Path]) -> None:
    tmp_path, import_mapping, popularity_file = temp_project_dir

    with (
        patch("depvex.resolver.IMPORT_MAPPING_FILE", import_mapping, create=True),
        patch("depvex.resolver.POPULARITY_MAPPING_FILE", popularity_file, create=True),
    ):
        resolver = DependencyResolver(root=str(tmp_path))
        pop_map = resolver.POPULARITY_MAPPING

        assert pop_map.get("aioboto3-stubs") == "types-aioboto3"
        assert pop_map.get("ai_core_sdk") == "sap-ai-sdk-core"


def test_resolve_uses_most_popular_package(
    temp_project_dir: tuple[Path, Path, Path],
) -> None:
    tmp_path, import_mapping, popularity_file = temp_project_dir

    with (
        patch("depvex.resolver.IMPORT_MAPPING_FILE", import_mapping, create=True),
        patch("depvex.resolver.POPULARITY_MAPPING_FILE", popularity_file, create=True),
        patch.object(DependencyResolver, "is_installed", return_value=False),
        patch.object(DependencyResolver, "get_local_version", return_value=None),
    ):
        resolver = DependencyResolver(root=str(tmp_path))

        resolved_pkg = resolver.resolve("aioboto3_stubs", has_net=False)

        assert resolved_pkg == "types-aioboto3"
        assert resolved_pkg != "types-aioboto3-lite"


def test_write_requirements_contains_popular_package(
    temp_project_dir: tuple[Path, Path, Path],
) -> None:
    tmp_path, import_mapping, popularity_file = temp_project_dir
    req_file = tmp_path / "requirements.txt"

    with (
        patch("depvex.resolver.IMPORT_MAPPING_FILE", import_mapping, create=True),
        patch("depvex.resolver.POPULARITY_MAPPING_FILE", popularity_file, create=True),
        patch.object(
            DependencyResolver,
            "discover_imports",
            return_value={"aioboto3_stubs", "ai_core_sdk"},
        ),
        patch.object(DependencyResolver, "is_installed", return_value=False),
        patch.object(DependencyResolver, "get_local_version", return_value=None),
    ):
        resolver = DependencyResolver(root=str(tmp_path))

        resolver._rebuild_single(root=str(tmp_path), output_path=str(req_file), prune_stale=False)

        written_content = req_file.read_text(encoding="utf-8").splitlines()

        assert any(line.startswith("types-aioboto3") for line in written_content)
        assert any(line.startswith("sap-ai-sdk-core") for line in written_content)
        assert not any(line.startswith("types-aioboto3-lite") for line in written_content)
        assert not any(line.startswith("ai-core-sdk") for line in written_content)
