import argparse
import sys
from pathlib import Path

from depvex.models.base_model import Colors
from depvex.writers import VALID_FORMATS, output_filename
from depvex.resolver import DependencyResolver
from depvex.sbom import SBOM_FORMATS
from depvex.watcher import ProjectWatcher


class DepvexCLI:
    def __init__(self) -> None:
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="depvex")
        commands = parser.add_mutually_exclusive_group(required=True)
        commands.add_argument(
            "--scan", action="store_const", const="scan", dest="command", help="Run a one-time dependency scan"
        )
        commands.add_argument(
            "--check", action="store_const", const="check", dest="command", help="Check requirements.txt is up to date"
        )
        commands.add_argument(
            "--watch", action="store_const", const="watch", dest="command", help="Watch and update requirements.txt"
        )
        commands.add_argument(
            "--report", action="store_const", const="report", dest="command", help="Report dependencies"
        )
        parser.add_argument("--pyproject", action="store_true", help="Also sync or check pyproject.toml dependencies")
        parser.add_argument(
            "--output-format",
            choices=sorted(VALID_FORMATS),
            default=None,
            dest="output_format",
            help=(
                "Format of the primary dependency file: "
                f"{', '.join(sorted(VALID_FORMATS))}. "
                "Default: auto-detect from the project, falling back to requirements.txt"
            ),
        )
        parser.add_argument(
            "--sbom", action="store_true", help="Also generate an SBOM file after scanning (--scan only)"
        )
        parser.add_argument(
            "--sbom-format",
            choices=sorted(SBOM_FORMATS),
            default="cyclonedx",
            dest="sbom_format",
            help="SBOM format to generate when --sbom is set (default: cyclonedx)",
        )
        parser.add_argument("path", nargs="?", default=".")
        return parser

    def _discover_imports(
        self, resolver: DependencyResolver, root: str, exclude_dirs: set[str] | None = None
    ) -> set[str]:
        return resolver.discover_imports(root, exclude_dirs=exclude_dirs)

    def _print_difference(
        self, resolver: DependencyResolver, current: list[str], expected: list[str], label: str
    ) -> None:
        current_by_name = {resolver._normalize_module_name(entry): entry for entry in current}
        expected_by_name = {resolver._normalize_module_name(entry): entry for entry in expected}
        missing = sorted(expected_by_name[name] for name in expected_by_name.keys() - current_by_name.keys())
        stale = sorted(current_by_name[name] for name in current_by_name.keys() - expected_by_name.keys())
        changed = sorted(
            (current_by_name[name], expected_by_name[name])
            for name in current_by_name.keys() & expected_by_name.keys()
            if current_by_name[name] != expected_by_name[name]
        )

        print(Colors.colorize(f"  [{label}] {output_filename(resolver.output_format)} is OUT OF DATE", Colors.YELLOW))
        for entry in missing:
            print(f"    missing: {entry}")
        for entry in stale:
            print(f"    stale: {entry}")
        for current_entry, expected_entry in changed:
            print(f"    changed: {current_entry} -> {expected_entry}")

    def _check_single(
        self,
        resolver: DependencyResolver,
        root: str,
        exclude_dirs: set[str] | None = None,
        output_path: Path | None = None,
        label: str = "root",
    ) -> bool:
        output_path = output_path or Path(resolver.default_output_path(root))
        expected_requirements = resolver.requirements_for(root, str(output_path), exclude_dirs=exclude_dirs)
        current_requirements = resolver._read_existing_requirements(str(output_path))
        if set(expected_requirements) == set(current_requirements):
            return True

        self._print_difference(resolver, current_requirements, expected_requirements, label)
        return False

    def _check_pyproject(self, resolver: DependencyResolver, root: str, expected: list[str], label: str) -> bool:
        # אם pyproject.toml הוא כבר הפורמט הראשי, הוא נבדק דרך _check_single - אין טעם לבדוק פעמיים
        if resolver.output_format == "pyproject":
            return True

        pyproject_path = Path(root) / "pyproject.toml"
        if not pyproject_path.exists():
            return True

        current = resolver.read_pyproject_dependencies(str(pyproject_path))
        if set(current) == set(expected):
            return True

        print(Colors.colorize(f"  [{label}] pyproject.toml dependencies are OUT OF DATE", Colors.YELLOW))
        self._print_difference(resolver, current, expected, label)
        return False

    def _sync_pyproject(self, resolver: DependencyResolver, root: str, dependencies: list[str]) -> None:
        # אם pyproject.toml הוא כבר הפורמט הראשי, הוא כבר סונכרן ב-write_req - אין טעם לכתוב פעמיים
        if resolver.output_format == "pyproject":
            return

        pyproject_path = Path(root) / "pyproject.toml"
        if pyproject_path.exists():
            resolver.write_pyproject_dependencies(str(pyproject_path), dependencies)
            print(Colors.colorize(f"[depvex] Updated {pyproject_path}", Colors.GREEN))

    def _write_sbom_for(self, resolver: DependencyResolver, path: str, requirements: dict | list, sbom_format: str) -> None:
        if isinstance(requirements, dict):
            for service, entries in requirements.items():
                service_root = path if service == "__root__" else str(Path(path) / service)
                sbom_path = resolver.generate_sbom(entries, output_dir=service_root, fmt=sbom_format)
                print(Colors.colorize(f"[depvex] SBOM written to {sbom_path}", Colors.GREEN))
        else:
            sbom_path = resolver.generate_sbom(requirements, output_dir=path, fmt=sbom_format)
            print(Colors.colorize(f"[depvex] SBOM written to {sbom_path}", Colors.GREEN))

    def scan(
        self,
        path: str,
        use_pyproject: bool = False,
        output_format: str | None = None,
        make_sbom: bool = False,
        sbom_format: str = "cyclonedx",
    ) -> int:
        print(Colors.colorize(f"[depvex] Starting one-time scan for {path}...", Colors.CYAN))
        resolver = DependencyResolver(root=path, output_format=output_format)
        requirements = resolver.rebuild_requirements(path)

        if use_pyproject:
            if isinstance(requirements, dict):
                for service, entries in requirements.items():
                    service_root = path if service == "__root__" else str(Path(path) / service)
                    self._sync_pyproject(resolver, service_root, entries)
            else:
                self._sync_pyproject(resolver, path, requirements)

        output_name = output_filename(resolver.output_format)
        if isinstance(requirements, dict):
            total = sum(len(entries) for entries in requirements.values())
            print(
                Colors.colorize(
                    f"[depvex] Updated {output_name} for {len(requirements)} service group(s), "
                    f"{total} dependency entries total:",
                    Colors.GREEN,
                )
            )
            for service, entries in requirements.items():
                label = "root" if service == "__root__" else service
                print(Colors.colorize(f"    - {label}: {len(entries)} entrie(s)", Colors.GREEN))
        else:
            print(
                Colors.colorize(
                    f"[depvex] Updated {output_name} with {len(requirements)} dependency entries.", Colors.GREEN
                )
            )

        if make_sbom:
            self._write_sbom_for(resolver, path, requirements, sbom_format)

        return 0

    def check(self, path: str, use_pyproject: bool = False, output_format: str | None = None) -> int:
        print(Colors.colorize(f"[depvex] Checking whether {path} is up to date...", Colors.CYAN))
        resolver = DependencyResolver(root=path, output_format=output_format)
        service_folders = resolver._get_active_service_folders(path)
        all_up_to_date = True
        output_name = output_filename(resolver.output_format)

        if service_folders:
            for service in service_folders:
                service_root = str(Path(path) / service)
                output_path = Path(resolver.default_output_path(service_root))
                up_to_date = self._check_single(resolver, service_root, output_path=output_path, label=service)
                status_color = Colors.GREEN if up_to_date else Colors.YELLOW
                status_text = "up to date" if up_to_date else "OUT OF DATE"
                if up_to_date:
                    print(Colors.colorize(f"  [{service}] {output_name} is {status_text}", status_color))
                all_up_to_date = all_up_to_date and up_to_date
                if use_pyproject:
                    expected = resolver.requirements_for(service_root, str(output_path))
                    all_up_to_date = self._check_pyproject(resolver, service_root, expected, service) and all_up_to_date

            root_output = Path(resolver.default_output_path(path))
            root_up_to_date = self._check_single(
                resolver, path, exclude_dirs=set(service_folders), output_path=root_output
            )
            status_color = Colors.GREEN if root_up_to_date else Colors.YELLOW
            status_text = "up to date" if root_up_to_date else "OUT OF DATE"
            if root_up_to_date:
                print(Colors.colorize(f"  [root] {output_name} is {status_text}", status_color))
            all_up_to_date = all_up_to_date and root_up_to_date
            if use_pyproject:
                expected = resolver.requirements_for(path, str(root_output), exclude_dirs=set(service_folders))
                all_up_to_date = self._check_pyproject(resolver, path, expected, "root") and all_up_to_date
        else:
            output_path = Path(resolver.default_output_path(path))
            if not output_path.exists():
                print(
                    Colors.colorize(
                        f"[depvex] No {output_name} found. Run 'depvex --scan .' first.", Colors.RED
                    )
                )
                return 1
            all_up_to_date = self._check_single(resolver, path, output_path=output_path)
            if use_pyproject:
                expected = resolver.requirements_for(path, str(output_path))
                all_up_to_date = self._check_pyproject(resolver, path, expected, "root") and all_up_to_date

        if not all_up_to_date:
            print(
                Colors.colorize(
                    f"[depvex] {output_name} is out of date somewhere. Run 'depvex --scan .' to update it.",
                    Colors.YELLOW,
                )
            )
            return 1

        print(Colors.colorize(f"[depvex] {output_name} is already up to date.", Colors.GREEN))
        return 0

    def report(self, path: str, output_format: str | None = None) -> int:
        resolver = DependencyResolver(root=path, output_format=output_format)
        service_folders = resolver._get_active_service_folders(path)
        groups: dict[str, list[str]] = {}

        if service_folders:
            for service in service_folders:
                service_root = str(Path(path) / service)
                groups[service] = resolver.requirements_for(service_root, resolver.default_output_path(service_root))
            groups["root"] = resolver.requirements_for(
                path, resolver.default_output_path(path), exclude_dirs=set(service_folders)
            )
        else:
            groups["root"] = resolver.requirements_for(path, resolver.default_output_path(path))

        print(Colors.colorize(f"[depvex] Dependency report for {path} (format: {resolver.output_format})", Colors.CYAN))
        for group, dependencies in groups.items():
            print(f"  [{group}] {len(dependencies)} dependency entries")
            for dependency in sorted(dependencies):
                print(f"    {dependency}")

        dependency_groups: dict[str, list[str]] = {}
        for group, dependencies in groups.items():
            for dependency in dependencies:
                dependency_groups.setdefault(resolver._normalize_module_name(dependency), []).append(group)
        shared = sorted(name for name, owners in dependency_groups.items() if len(owners) > 1)
        if shared:
            print("  [shared]")
            for dependency in shared:
                print(f"    {dependency}: {', '.join(sorted(dependency_groups[dependency]))}")
        return 0

    def watch(self, path: str, output_format: str | None = None) -> None:
        print(Colors.colorize(f"[depvex] Starting watch mode for {path}...", Colors.CYAN))
        resolver = DependencyResolver(root=path, output_format=output_format)
        print(
            Colors.colorize(
                f"[depvex] Depvex will keep scanning and updating {output_filename(resolver.output_format)} "
                "as files change.",
                Colors.YELLOW,
            )
        )

        resolver.rebuild_requirements(path)
        ProjectWatcher(path, resolver=resolver).start()

    def run(self, argv: list[str] | None = None) -> int:
        args = self.parser.parse_args(argv or sys.argv[1:])

        if args.command == "scan":
            return self.scan(
                args.path,
                use_pyproject=args.pyproject,
                output_format=args.output_format,
                make_sbom=args.sbom,
                sbom_format=args.sbom_format,
            )

        if args.command == "check":
            return self.check(args.path, use_pyproject=args.pyproject, output_format=args.output_format)

        if args.command == "watch":
            self.watch(args.path, output_format=args.output_format)
            return 0

        if args.command == "report":
            return self.report(args.path, output_format=args.output_format)

        self.parser.print_help()
        return 1

    def __call__(self, argv: list[str] | None = None) -> int:
        return self.run(argv)


def main(argv: list[str] | None = None) -> int:
    return DepvexCLI().run(argv)