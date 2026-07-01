from pathlib import Path
import argparse
import sys

from depvex.watcher import ProjectWatcher
from depvex.resolver import DependencyResolver

class DepvexCLI:
    def __init__(self) -> None:
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="depvex")
        subparsers = parser.add_subparsers(dest="command")

        watch_parser = subparsers.add_parser("watch", help="Watch project and auto-update requirements.txt")
        watch_parser.add_argument("path", nargs="?", default=".")
        return parser

    def watch(self, path: str) -> None:
        print(f"[depvex] Watching {path} ...")

        resolver = DependencyResolver()
        resolver.rebuild_requirements(path)
        ProjectWatcher(path, resolver=resolver).start()

    def run(self, argv: list[str] | None = None) -> int:
        args = self.parser.parse_args(argv or sys.argv[1:])

        if args.command == "watch":
            self.watch(args.path)
            return 0

        self.parser.print_help()
        return 1

    def __call__(self, argv: list[str] | None = None) -> int:
        return self.run(argv)


def main(argv: list[str] | None = None) -> int:
    return DepvexCLI().run(argv)