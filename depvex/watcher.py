import os
import threading
import time

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from depvex.resolver import DependencyResolver  # ignore depvex
from depvex.utils.read_config import project_config  # ignore depvex

DEFAULT_DEBOUNCE_SECONDS = getattr(project_config, "debounce_seconds", 1.5)


class ProjectFileHandler(FileSystemEventHandler):
    def __init__(
        self, root: str, resolver: DependencyResolver | None = None, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS
    ) -> None:
        super().__init__()
        self.root = root
        self.resolver = resolver or DependencyResolver()
        self.debounce_seconds = debounce_seconds
        self._pending_files: set[str] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule_run(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()

            self._timer = threading.Timer(self.debounce_seconds, self._process_pending)
            self._timer.daemon = True
            self._timer.start()

    def _service_for_file(self, file_path: str, service_folders: list[str]) -> str | None:
        if not service_folders:
            return None

        rel_path = os.path.relpath(file_path, self.root)
        first_part = rel_path.split(os.sep)[0]
        return first_part if first_part in service_folders else None

    def _process_pending(self) -> None:
        with self._lock:
            pending_files = list(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        if not pending_files:
            return

        service_folders = self.resolver._get_active_service_folders(self.root)

        if not service_folders:
            print(f"[depvex] idle detected → full rescan after {self.debounce_seconds}s")
            self.resolver.rebuild_requirements(self.root)
            return

        affected_services: set[str] = set()
        root_changed = False

        for file_path in pending_files:
            service = self._service_for_file(file_path, service_folders)
            if service:
                affected_services.add(service)
            else:
                root_changed = True

        for service in affected_services:
            service_root = os.path.join(self.root, service)
            print(f"[depvex] idle detected → rescan for service '{service}'")
            self.resolver._rebuild_single(service_root, os.path.join(service_root, "requirements.txt"))

        if root_changed:
            print("[depvex] idle detected → rescan for root")
            self.resolver._rebuild_single(
                self.root, os.path.join(self.root, "requirements.txt"), exclude_dirs=set(service_folders)
            )

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        with self._lock:
            self._pending_files.add(event.src_path)

        self._schedule_run()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        with self._lock:
            self._pending_files.add(event.src_path)

        self._schedule_run()


class ProjectWatcher:
    def __init__(
        self, root: str, resolver: DependencyResolver | None = None, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS
    ) -> None:
        self.root = root
        self.resolver = resolver or DependencyResolver()
        self.debounce_seconds = debounce_seconds

    def start(self) -> None:
        print("[depvex] watching:", self.root)

        event_handler = ProjectFileHandler(self.root, self.resolver, debounce_seconds=self.debounce_seconds)
        observer = Observer()
        observer.schedule(event_handler, self.root, recursive=True)
        observer.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()

        observer.join()
