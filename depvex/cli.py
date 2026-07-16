from pathlib import Path
import argparse
import sys

from depvex.resolver import DependencyResolver # ignore depvex
from depvex.watcher import ProjectWatcher # ignore depvex

from depvex.models.base_model import Colors # ignore depvex


class DepvexCLI:
    def __init__(self) -> None:
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="depvex")
        subparsers = parser.add_subparsers(dest="command")

        scan_parser = subparsers.add_parser("--scan", help="Run a one-time dependency scan and update requirements.txt")
        scan_parser.add_argument("path", nargs="?", default=".")

        check_parser = subparsers.add_parser("--check", help="Check whether requirements.txt is up to date")
        check_parser.add_argument("path", nargs="?", default=".")

        watch_parser = subparsers.add_parser("--watch", help="Watch project and auto-update requirements.txt")
        watch_parser.add_argument("path", nargs="?", default=".")
        return parser

    def _discover_imports(self, resolver: DependencyResolver, root: str, exclude_dirs: set[str] | None = None) -> set[str]:
        discovered = set()
        for file_path in resolver._walk_python_files(root, exclude_dirs=exclude_dirs):
            discovered.update(resolver._get_imports_for_file(file_path))
        return discovered

    def _check_single(
        self,
        resolver: DependencyResolver,
        root: str,
        exclude_dirs: set[str] | None = None,
        output_path: Path | None = None,
    ) -> bool:
        output_path = output_path or (Path(root) / "requirements.txt")

        if not output_path.exists():
            return False

        discovered = self._discover_imports(resolver, root, exclude_dirs=exclude_dirs)
        expected_requirements = [
            resolver.resolve(module_name, resolver.internet_check())
            for module_name in sorted(discovered)
            if module_name
        ]
        current_requirements = [
            line.strip() for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

        return set(expected_requirements) == set(current_requirements)

    def scan(self, path: str) -> int:
        print(Colors.colorize(f"[depvex] Starting one-time scan for {path}...", Colors.CYAN))
        resolver = DependencyResolver()
        requirements = resolver.rebuild_requirements(path)

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

    def check(self, path: str) -> int:
        print(Colors.colorize(f"[depvex] Checking whether {path} is up to date...", Colors.CYAN))
        resolver = DependencyResolver()
        service_folders = resolver._get_active_service_folders(path)
        all_up_to_date = True

        if service_folders:
            for service in service_folders:
                service_root = str(Path(path) / service)
                output_path = Path(service_root) / "requirements.txt"
                up_to_date = self._check_single(resolver, service_root, output_path=output_path)
                status_color = Colors.GREEN if up_to_date else Colors.YELLOW
                status_text = "up to date" if up_to_date else "OUT OF DATE"
                print(Colors.colorize(f"  [{service}] requirements.txt is {status_text}", status_color))
                all_up_to_date = all_up_to_date and up_to_date

            root_output = Path(path) / "requirements.txt"
            root_up_to_date = self._check_single(
                resolver, path, exclude_dirs=set(service_folders), output_path=root_output
            )
            status_color = Colors.GREEN if root_up_to_date else Colors.YELLOW
            status_text = "up to date" if root_up_to_date else "OUT OF DATE"
            print(Colors.colorize(f"  [root] requirements.txt is {status_text}", status_color))
            all_up_to_date = all_up_to_date and root_up_to_date
        else:
            output_path = Path(path) / "requirements.txt"
            if not output_path.exists():
                print(Colors.colorize("[depvex] No requirements.txt found. Run 'depvex scan .' first.", Colors.RED))
                return 1
            all_up_to_date = self._check_single(resolver, path, output_path=output_path)

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
            return self.scan(args.path)

        if args.command == "check":
            return self.check(args.path)

        if args.command == "watch":
            self.watch(args.path)
            return 0

        self.parser.print_help()
        return 1

    def __call__(self, argv: list[str] | None = None) -> int:
        return self.run(argv)


def main(argv: list[str] | None = None) -> int:
    return DepvexCLI().run(argv)