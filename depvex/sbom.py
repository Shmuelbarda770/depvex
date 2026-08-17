"""SBOM (Software Bill of Materials) generation.

Supported formats:
  - CycloneDX 1.5 JSON  (default, ``sbom.cdx.json``)
  - SPDX 2.3 JSON       (``sbom.spdx.json``)
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone


def _parse_requirements(requirements: list[str]) -> list[tuple[str, str | None]]:
    """Parse requirement strings → [(name, version_or_None)]."""
    result: list[tuple[str, str | None]] = []
    for req in requirements:
        req = req.strip()
        if not req:
            continue
        if "==" in req:
            name, version = req.split("==", 1)
            result.append((name.strip(), version.strip()))
        else:
            name = re.split(r"[><=!~]", req, maxsplit=1)[0].strip()
            result.append((name, None))
    return result


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── SPDX 2.3 ─────────────────────────────────────────────────────────────────


def generate_spdx(
    requirements: list[str],
    project_name: str = "my-project",
    project_version: str = "0.0.0",
    licenses: dict[str, str] | None = None,
) -> dict:
    """Generate an SPDX 2.3 JSON document.

    Args:
        requirements: Requirement strings (e.g. ``['flask==3.1.3', 'click']``).
        project_name: Name embedded in the document.
        project_version: Version of the project being described.
        licenses: Optional mapping of ``{package_name: spdx_id}``.
    """
    pkgs = _parse_requirements(requirements)
    doc_ns = f"https://spdx.org/spdxdocs/{project_name}-{uuid.uuid4()}"
    lic_map = licenses or {}

    spdx_packages: list[dict] = [
        {
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": project_name,
            "versionInfo": project_version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
        }
    ]
    relationships: list[dict] = []

    for name, ver in pkgs:
        spdx_id = "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]", "-", name)
        lic_id = lic_map.get(name, "NOASSERTION")
        spdx_packages.append({
            "SPDXID": spdx_id,
            "name": name,
            "versionInfo": ver or "NOASSERTION",
            "downloadLocation": f"https://pypi.org/project/{name}/{ver or ''}",
            "filesAnalyzed": False,
            "licenseConcluded": lic_id,
            "licenseDeclared": lic_id,
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": (
                        f"pkg:pypi/{name.lower()}@{ver}" if ver else f"pkg:pypi/{name.lower()}"
                    ),
                }
            ],
        })
        relationships.append({
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": spdx_id,
        })

    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": project_name,
        "documentNamespace": doc_ns,
        "creationInfo": {"created": _now_utc(), "creators": ["Tool: depvex"]},
        "packages": spdx_packages,
        "relationships": relationships,
    }


# ── CycloneDX 1.5 ────────────────────────────────────────────────────────────


def generate_cyclonedx(
    requirements: list[str],
    project_name: str = "my-project",
    project_version: str = "0.0.0",
    licenses: dict[str, str] | None = None,
) -> dict:
    """Generate a CycloneDX 1.5 JSON BOM.

    Args:
        requirements: Requirement strings.
        project_name: Application component name.
        project_version: Application component version.
        licenses: Optional mapping of ``{package_name: spdx_id}``.
    """
    pkgs = _parse_requirements(requirements)
    lic_map = licenses or {}
    components: list[dict] = []

    for name, ver in pkgs:
        purl = f"pkg:pypi/{name.lower()}@{ver}" if ver else f"pkg:pypi/{name.lower()}"
        component: dict = {"type": "library", "name": name, "purl": purl}
        if ver:
            component["version"] = ver
        lic_id = lic_map.get(name)
        if lic_id and lic_id not in {"NOASSERTION", "UNKNOWN"}:
            component["licenses"] = [{"license": {"id": lic_id}}]
        components.append(component)

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "metadata": {
            "timestamp": _now_utc(),
            "tools": [{"vendor": "depvex", "name": "depvex"}],
            "component": {"type": "application", "name": project_name, "version": project_version},
        },
        "components": components,
    }


# ── Writer ────────────────────────────────────────────────────────────────────

SBOM_FORMATS = {"cyclonedx", "spdx"}


def write_sbom(
    requirements: list[str],
    output_dir: str = ".",
    fmt: str = "cyclonedx",
    project_name: str | None = None,
    licenses: dict[str, str] | None = None,
) -> str:
    """Generate and write an SBOM file, returning the output path."""
    if project_name is None:
        project_name = os.path.basename(os.path.abspath(output_dir)) or "project"

    fmt = fmt.lower()
    if fmt == "spdx":
        data = generate_spdx(requirements, project_name, licenses=licenses)
        filename = "sbom.spdx.json"
    else:
        data = generate_cyclonedx(requirements, project_name, licenses=licenses)
        filename = "sbom.cdx.json"

    output_path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return output_path
