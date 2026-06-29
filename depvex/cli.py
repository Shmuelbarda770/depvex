import typer
from depvex.watcher import start_watching
app = typer.Typer()

@app.command()
def watch(path: str = "."):
    """Watch project and auto-update requirements.txt"""
    print(f"[depvex] Watching {path} ...")
    start_watching(path)

def main():
    app()