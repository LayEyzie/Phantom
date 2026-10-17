"""Shared setup for the headless tests: a fake customtkinter and a throw-away APPDATA."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = tempfile.mkdtemp()
os.environ["APPDATA"] = tmp
sys.argv = ["phantom.py"]
sys.path.insert(0, str(ROOT / "tests" / "stubs"))
sys.path.insert(0, str(ROOT))
threading.excepthook = lambda args: None  # banner downloads simply fail without a network

spec = importlib.util.spec_from_file_location("phantom", ROOT / "phantom.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import customtkinter as ctk  # noqa: E402  (the stub)


def boot(games=None, installed=None, dll=True):
    """Builds the App with the fake toolkit and returns it."""
    if games is not None:
        m.CONFIG.write_text(json.dumps(games))
    m.scan_installed = lambda: installed
    m.find_dll = (lambda: Path("/x/steam_api64.dll")) if dll else (lambda: None)
    ctk.INSTANCES.clear()
    ctk.AFTERS.clear()
    m.run_gui()
    return ctk.INSTANCES[-1]
