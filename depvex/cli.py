import argparse
import os
import sys
from pathlib import Path

from depvex.resolver import DependencyResolver
from depvex.watcher import ProjectWatcher


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    @classmethod
    def enabled(cls) -> bool:
        return os.getenv("NO_COLOR") is None and os.getenv("TERM") not in {None, "dumb"}

    @classmethod
    def colorize(cls, text: str, color: str) -> str:
        return f"{color}{text}{cls.RESET}" if cls.enabled() else text


class DepvexCLI:
    def __init__(self) -> None:
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="depvex")
        subparsers = parser.add_subparsers(dest="command")

        scan_parser = subparsers.add_parser("scan", help="Run a one-time dependency scan and update requirements.txt")
        scan_parser.add_argument("path", nargs="?", default=".")

        check_parser = subparsers.add_parser("check", help="Check whether requirements.txt is up to date")
        check_parser.add_argument("path", nargs="?", default=".")

        watch_parser = subparsers.add_parser("watch", help="Watch project and auto-update requirements.txt")
        watch_parser.add_argument("path", nargs="?", default=".")
        return parser

    def scan(self, path: str) -> int:
        print(Colors.colorize(f"[depvex] Starting one-time scan for {path}...", Colors.CYAN))
        resolver = DependencyResolver()
        requirements = resolver.rebuild_requirements(path)
        print(
            Colors.colorize(
                f"[depvex] Updated requirements.txt with {len(requirements)} dependency entries.", Colors.GREEN
            )
        )
        return 0

    def check(self, path: str) -> int:
        print(Colors.colorize(f"[depvex] Checking whether {path}/requirements.txt is up to date...", Colors.CYAN))
        resolver = DependencyResolver()
        output_path = Path(path) / "requirements.txt"

        if not output_path.exists():
            print(Colors.colorize("[depvex] No requirements.txt found. Run 'depvex scan .' first.", Colors.RED))
            return 1

        discovered = set()
        for dirpath, dirnames, filenames in __import__("os").walk(path):
            dirnames[:] = [
                directory
                for directory in dirnames
                if directory not in {".git", "__pycache__", ".venv", "venv", "node_modules"}
            ]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                file_path = Path(dirpath) / filename
                discovered.update(resolver._get_imports_for_file(str(file_path)))

        expected_requirements = [
            resolver.resolve(module_name, resolver.internet_check())
            for module_name in sorted(discovered)
            if module_name
        ]
        current_requirements = [
            line.strip() for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

        if set(expected_requirements) != set(current_requirements):
            print(
                Colors.colorize(
                    "[depvex] requirements.txt is out of date. Run 'depvex scan .' to update it.", Colors.YELLOW
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
