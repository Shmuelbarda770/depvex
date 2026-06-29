import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from depvex.parser import extract_imports
from depvex.resolver import rebuild_requirements


class Handler(FileSystemEventHandler):
    def __init__(self, root):
        self.root = root

    def on_modified(self, event):
        if event.is_directory:
            return

        if event.src_path.endswith(".py"):
            print(f"[depvex] change detected → full rescan")
            rebuild_requirements(self.root)


def start_watching(path):
    print("[depvex] watching:", path)

    event_handler = Handler(path)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()