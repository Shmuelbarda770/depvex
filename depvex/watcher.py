import threading
import time

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from depvex.resolver import DependencyResolver
from depvex.utils.read_config import project_config

class ProjectFileHandler(FileSystemEventHandler):
    def __init__(self, root: str, resolver: DependencyResolver | None = None, debounce_seconds: float = 1.5) -> None:
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

    def _process_pending(self) -> None:
        with self._lock:
            pending_files = list(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        if not pending_files:
            return

        print(f"[depvex] idle detected → full rescan after {self.debounce_seconds}s")
        self.resolver.rebuild_requirements(self.root)

    def on_modified(self, event:FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        with self._lock:
            self._pending_files.add(event.src_path)

        self._schedule_run()

    def on_created(self, event:FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        with self._lock:
            self._pending_files.add(event.src_path)

        self._schedule_run()


class ProjectWatcher:
    def __init__(self, root: str, resolver: DependencyResolver | None = None, debounce_seconds: float = project_config.debounce_seconds) -> None:
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
