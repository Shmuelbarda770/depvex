import argparse
import sys
from pathlib import Path

from depvex.models.base_model import Colors  # ignore depvex
from depvex.resolver import DependencyResolver  # ignore depvex
from depvex.watcher import ProjectWatcher  # ignore depvex


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

        print(Colors.colorize(f"  [{label}] requirements.txt is OUT OF DATE", Colors.YELLOW))
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
        output_path = output_path or (Path(root) / "requirements.txt")
        expected_requirements = resolver.requirements_for(root, str(output_path), exclude_dirs=exclude_dirs)
        current_requirements = resolver._read_existing_requirements(str(output_path))
        if set(expected_requirements) == set(current_requirements):
            return True

        self._print_difference(resolver, current_requirements, expected_requirements, label)
        return False

    def _check_pyproject(self, resolver: DependencyResolver, root: str, expected: list[str], label: str) -> bool:
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
        pyproject_path = Path(root) / "pyproject.toml"
        if pyproject_path.exists():
            resolver.write_pyproject_dependencies(str(pyproject_path), dependencies)
            print(Colors.colorize(f"[depvex] Updated {pyproject_path}", Colors.GREEN))

    def scan(self, path: str, use_pyproject: bool = False) -> int:
        print(Colors.colorize(f"[depvex] Starting one-time scan for {path}...", Colors.CYAN))
        resolver = DependencyResolver()
        requirements = resolver.rebuild_requirements(path)

        if use_pyproject:
            if isinstance(requirements, dict):
                for service, entries in requirements.items():
                    service_root = path if service == "__root__" else str(Path(path) / service)
                    self._sync_pyproject(resolver, service_root, entries)
            else:
                self._sync_pyproject(resolver, path, requirements)

        if isinstance(requirements, dict):
            total = sum(len(entries) for entries in requirements.values())
            print(
                Colors.colorize(
                    f"[depvex] Updated requirements.txt for {len(requirements)} service group(s), "
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
                    f"[depvex] Updated requirements.txt with {len(requirements)} dependency entries.", Colors.GREEN
                )
            )

        return 0

    def check(self, path: str, use_pyproject: bool = False) -> int:
        print(Colors.colorize(f"[depvex] Checking whether {path} is up to date...", Colors.CYAN))
        resolver = DependencyResolver()
        service_folders = resolver._get_active_service_folders(path)
        all_up_to_date = True

        if service_folders:
            for service in service_folders:
                service_root = str(Path(path) / service)
                output_path = Path(service_root) / "requirements.txt"
                up_to_date = self._check_single(resolver, service_root, output_path=output_path, label=service)
                status_color = Colors.GREEN if up_to_date else Colors.YELLOW
                status_text = "up to date" if up_to_date else "OUT OF DATE"
                if up_to_date:
                    print(Colors.colorize(f"  [{service}] requirements.txt is {status_text}", status_color))
                all_up_to_date = all_up_to_date and up_to_date
                if use_pyproject:
                    expected = resolver.requirements_for(service_root, str(output_path))
                    all_up_to_date = self._check_pyproject(resolver, service_root, expected, service) and all_up_to_date

            root_output = Path(path) / "requirements.txt"
            root_up_to_date = self._check_single(
                resolver, path, exclude_dirs=set(service_folders), output_path=root_output
            )
            status_color = Colors.GREEN if root_up_to_date else Colors.YELLOW
            status_text = "up to date" if root_up_to_date else "OUT OF DATE"
            if root_up_to_date:
                print(Colors.colorize(f"  [root] requirements.txt is {status_text}", status_color))
            all_up_to_date = all_up_to_date and root_up_to_date
            if use_pyproject:
                expected = resolver.requirements_for(path, str(root_output), exclude_dirs=set(service_folders))
                all_up_to_date = self._check_pyproject(resolver, path, expected, "root") and all_up_to_date
        else:
            output_path = Path(path) / "requirements.txt"
            if not output_path.exists():
                print(Colors.colorize("[depvex] No requirements.txt found. Run 'depvex scan .' first.", Colors.RED))
                return 1
            all_up_to_date = self._check_single(resolver, path, output_path=output_path)
            if use_pyproject:
                expected = resolver.requirements_for(path, str(output_path))
                all_up_to_date = self._check_pyproject(resolver, path, expected, "root") and all_up_to_date

        if not all_up_to_date:
            print(
                Colors.colorize(
                    "[depvex] requirements.txt is out of date somewhere. Run 'depvex scan .' to update it.",
                    Colors.YELLOW,
                )
            )
            return 1

        print(Colors.colorize("[depvex] requirements.txt is already up to date.", Colors.GREEN))
        return 0

    def report(self, path: str) -> int:
        resolver = DependencyResolver()
        service_folders = resolver._get_active_service_folders(path)
        groups: dict[str, list[str]] = {}

        if service_folders:
            for service in service_folders:
                service_root = str(Path(path) / service)
                groups[service] = resolver.requirements_for(service_root, str(Path(service_root) / "requirements.txt"))
            groups["root"] = resolver.requirements_for(
                path, str(Path(path) / "requirements.txt"), exclude_dirs=set(service_folders)
            )
        else:
            groups["root"] = resolver.requirements_for(path, str(Path(path) / "requirements.txt"))

        print(Colors.colorize(f"[depvex] Dependency report for {path}", Colors.CYAN))
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

    def watch(self, path: str) -> None:
        print(Colors.colorize(f"[depvex] Starting watch mode for {path}...", Colors.CYAN))
        print(
            Colors.colorize(
                "[depvex] Depvex will keep scanning and updating requirements.txt as files change.", Colors.YELLOW
            )
        )

        resolver = DependencyResolver()
        resolver.rebuild_requirements(path)
        ProjectWatcher(path, resolver=resolver).start()

    def run(self, argv: list[str] | None = None) -> int:
        args = self.parser.parse_args(argv or sys.argv[1:])

        if args.command == "scan":
            return self.scan(args.path, use_pyproject=args.pyproject)

        if args.command == "check":
            return self.check(args.path, use_pyproject=args.pyproject)

        if args.command == "watch":
            self.watch(args.path)
            return 0

        if args.command == "report":
            return self.report(args.path)

        self.parser.print_help()
        return 1

    def __call__(self, argv: list[str] | None = None) -> int:
        return self.run(argv)


def main(argv: list[str] | None = None) -> int:
    return DepvexCLI().run(argv)
