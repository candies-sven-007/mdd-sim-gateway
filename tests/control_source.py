"""The control plane's Python source, for tests that check the code itself.

main.py was split into state.py, tasks.py and routers/*.py; a test that searched main.py alone
would silently check less. Files are joined in a fixed order, each ending with a newline.
"""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "control" / "app"


def control_plane_files():
    names = [APP / "main.py", APP / "state.py", APP / "tasks.py"]
    names += sorted((APP / "routers").glob("*.py"))
    return [path for path in names if path.exists()]


def control_plane_source() -> str:
    return "".join(path.read_text(encoding="utf-8").rstrip("\n") + "\n"
                   for path in control_plane_files())
