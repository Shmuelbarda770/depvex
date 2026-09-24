import json
import sys
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
from packaging.markers import default_environment

try:
    import requests
except ImportError:
    requests = None


@dataclass(frozen=True)
class CompatibilityIssue:
    severity: str
    code: str
    package: str
    message: str
    requirement: str | None = None
    source: str | None = None


@dataclass
class CompatibilityReport:
    target: str
    candidate_version: str | None = None
    compatible: bool = True
    uncertain: bool = False
    issues: list[CompatibilityIssue] = field(default_factory=list)
    examined_packages: list[str] = field(default_factory=list)

    def add_issue(self, issue: CompatibilityIssue) -> None:
        self.issues.append(issue)
        if issue.severity == "error":
            self.compatible = False
        if issue.severity == "warning":
            self.uncertain = True

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = [asdict(issue) for issue in self.issues]
        return result


class CompatibilityAnalyzer:
    """Analyze a package addition without modifying the current environment."""

    def __init__(self, root: str = ".", allow_network: bool = True, timeout: int = 5) -> None:
        self.root = Path(root).resolve()
        self.allow_network = allow_network
        self.timeout = timeout
        self._metadata_cache: dict[str, dict[str, Any] | None] = {}
        self._versions_cache: dict[str, list[Version]] = {}

    @staticmethod
    def _normalise(name: str) -> str:
        return name.lower().replace("_", "-").replace(".", "-")

    def _read_existing_requirements(self) -> list[tuple[Requirement, str, str]]:
        entries: list[tuple[Requirement, str, str]] = []
        paths = [self.root / "requirements.txt"]
        paths.extend(sorted(self.root.glob("requirements-*.txt")))
        for path in paths:
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, raw_line in enumerate(lines, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(("-r", "--", "-e", "git+")):
                    continue
                try:
                    requirement = Requirement(line)
                    if self._marker_is_active(requirement):
                        entries.append((requirement, line, f"{path}:{line_number}"))
                except InvalidRequirement:
                    continue
        return entries

    def _local_metadata(self, package: str) -> dict[str, Any] | None:
        try:
            info = distribution(package)
        except PackageNotFoundError:
            return None
        return {"version": info.version, "requires_dist": list(info.requires or [])}

    def _pypi_metadata(self, package: str, version: str | None = None) -> dict[str, Any] | None:
        if not self.allow_network or requests is None:
            return None
        try:
            endpoint = f"https://pypi.org/pypi/{package}/{version}/json" if version else f"https://pypi.org/pypi/{package}/json"
            response = requests.get(endpoint, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            info = data.get("info", {})
            return {
                "version": info.get("version"),
                "requires_dist": info.get("requires_dist") or [],
                "releases": data.get("releases", {}),
            }
        except (requests.RequestException, ValueError, TypeError):
            return None

    def _metadata(self, package: str) -> dict[str, Any] | None:
        key = self._normalise(package)
        if key not in self._metadata_cache:
            self._metadata_cache[key] = self._local_metadata(package) or self._pypi_metadata(package)
        return self._metadata_cache[key]

    @staticmethod
    def _marker_is_active(requirement: Requirement, extras: set[str] | None = None) -> bool:
        if requirement.marker is None:
            return True
        environment = default_environment()
        environment["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        active_extras = extras or {""}
        return any(requirement.marker.evaluate({**environment, "extra": extra}) for extra in active_extras)

    def _available_versions(self, package: str) -> list[Version]:
        key = self._normalise(package)
        if key in self._versions_cache:
            return self._versions_cache[key]
        metadata = self._metadata(package)
        versions: list[Version] = []
        if metadata:
            for raw_version in metadata.get("releases", {}):
                try:
                    versions.append(Version(raw_version))
                except InvalidVersion:
                    continue
            if not versions and metadata.get("version"):
                try:
                    versions.append(Version(str(metadata["version"])))
                except InvalidVersion:
                    pass
        self._versions_cache[key] = sorted(set(versions), reverse=True)
        return self._versions_cache[key]

    def _specifier_intersects(self, package: str, left: SpecifierSet, right: SpecifierSet) -> bool | None:
        combined = SpecifierSet(f"{left},{right}")
        exact_versions = []
        for specifier_set in (left, right):
            exact_versions.extend(
                Version(specifier.version)
                for specifier in specifier_set
                if specifier.operator == "==" and "*" not in specifier.version
            )
        if exact_versions:
            return any(version in combined for version in exact_versions)
        for version in self._available_versions(package):
            if version in combined:
                return True
        metadata = self._metadata(package)
        if metadata is not None and "releases" in metadata:
            return False
        return None

    def _select_candidate(self, requirement: Requirement) -> tuple[Version | None, dict[str, Any] | None]:
        metadata = self._metadata(requirement.name)
        if metadata is None:
            return None, None
        raw_version = metadata.get("version")
        if not raw_version:
            return None, metadata
        try:
            candidate = Version(str(raw_version))
        except InvalidVersion:
            return None, metadata
        if requirement.specifier and candidate not in requirement.specifier:
            if self.allow_network and requests is not None:
                version_metadata = self._pypi_metadata(requirement.name)
                if version_metadata is not None:
                    metadata = version_metadata
                    self._metadata_cache[self._normalise(requirement.name)] = version_metadata
            candidates = [version for version in self._available_versions(requirement.name) if version in requirement.specifier]
            candidate = candidates[0] if candidates else None
            if candidate is not None and self.allow_network and requests is not None:
                version_metadata = self._pypi_metadata(requirement.name, str(candidate))
                if version_metadata is not None:
                    metadata = version_metadata
        return candidate, metadata

    def analyze(self, target: str) -> CompatibilityReport:
        try:
            target_requirement = Requirement(target)
        except InvalidRequirement as exc:
            report = CompatibilityReport(target=target, compatible=False)
            report.add_issue(CompatibilityIssue("error", "invalid-target", target, str(exc)))
            return report

        report = CompatibilityReport(target=target)
        existing = self._read_existing_requirements()
        existing_by_name: dict[str, list[tuple[Requirement, str, str]]] = {}
        for requirement, raw, source in existing:
            existing_by_name.setdefault(self._normalise(requirement.name), []).append((requirement, raw, source))

        target_name = self._normalise(target_requirement.name)
        for requirement, raw, source in existing_by_name.get(target_name, []):
            intersection = self._specifier_intersects(
                target_requirement.name, target_requirement.specifier, requirement.specifier
            )
            if intersection is False:
                report.add_issue(
                    CompatibilityIssue(
                        "error",
                        "direct-conflict",
                        requirement.name,
                        f"requested {target} conflicts with existing requirement {raw}",
                        requirement=raw,
                        source=source,
                    )
                )

        candidate, metadata = self._select_candidate(target_requirement)
        if candidate is None:
            report.add_issue(
                CompatibilityIssue(
                    "warning",
                    "candidate-unknown",
                    target_requirement.name,
                    "could not determine a candidate version from local metadata or PyPI",
                    requirement=target,
                )
            )
            return report

        report.candidate_version = str(candidate)
        queue: list[tuple[str, str, list[str], dict[str, Any] | None, set[str]]] = [
            (target_requirement.name, str(candidate), [target_requirement.name], metadata, set(target_requirement.extras))
        ]
        visited: set[tuple[str, str]] = set()

        while queue and len(report.examined_packages) < 256:
            package, package_version, chain, package_metadata, package_extras = queue.pop(0)
            visit_key = (self._normalise(package), package_version)
            if visit_key in visited:
                continue
            visited.add(visit_key)
            report.examined_packages.append(f"{package}=={package_version}")
            if package_metadata is None:
                report.add_issue(
                    CompatibilityIssue(
                        "warning",
                        "dependency-metadata-unknown",
                        package,
                        f"metadata unavailable while examining {' -> '.join(chain)}",
                        source=" -> ".join(chain),
                    )
                )
                continue

            for raw_dependency in package_metadata.get("requires_dist", []):
                try:
                    dependency = Requirement(raw_dependency)
                except InvalidRequirement:
                    continue
                if not self._marker_is_active(dependency, package_extras):
                    continue
                dependency_name = self._normalise(dependency.name)
                for existing_requirement, raw, source in existing_by_name.get(dependency_name, []):
                    intersection = self._specifier_intersects(
                        dependency.name, dependency.specifier, existing_requirement.specifier
                    )
                    if intersection is False:
                        report.add_issue(
                            CompatibilityIssue(
                                "error",
                                "transitive-conflict",
                                dependency.name,
                                f"{package}=={package_version} requires {raw_dependency}, "
                                f"but the project declares {raw}",
                                requirement=raw,
                                source=source,
                            )
                        )
                    elif intersection is None:
                        report.add_issue(
                            CompatibilityIssue(
                                "warning",
                                "constraint-unknown",
                                dependency.name,
                                f"could not prove whether {raw_dependency} is compatible with {raw}",
                                requirement=raw,
                                source=source,
                            )
                        )

                installed = self._local_metadata(dependency.name)
                if installed and installed.get("version"):
                    try:
                        installed_version = Version(str(installed["version"]))
                    except InvalidVersion:
                        installed_version = None
                    if installed_version is not None and dependency.specifier and installed_version not in dependency.specifier:
                        report.add_issue(
                            CompatibilityIssue(
                                "warning",
                                "installed-version-change",
                                dependency.name,
                                f"candidate requires {raw_dependency}, but installed version is {installed_version}",
                                requirement=raw_dependency,
                                source=" -> ".join(chain),
                            )
                        )

                dependency_candidate, dependency_metadata = self._select_candidate(dependency)
                if dependency_candidate is None:
                    report.add_issue(
                        CompatibilityIssue(
                            "warning",
                            "dependency-metadata-unknown",
                            dependency.name,
                            f"could not resolve metadata for {raw_dependency} while examining {' -> '.join(chain)}",
                            requirement=raw_dependency,
                            source=" -> ".join(chain),
                        )
                    )
                else:
                    queue.append(
                        (
                            dependency.name,
                            str(dependency_candidate),
                            [*chain, dependency.name],
                            dependency_metadata,
                            set(dependency.extras),
                        )
                    )

        if len(report.examined_packages) >= 256:
            report.add_issue(
                CompatibilityIssue(
                    "warning", "analysis-limit", target_requirement.name, "dependency graph exceeded 256 packages"
                )
            )
        return report


def report_as_json(report: CompatibilityReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)