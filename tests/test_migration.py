"""Data folders of earlier app names are renamed to the current one on first start."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv = ["phantom.py"]


def load(name, appdata):
    os.environ["APPDATA"] = str(appdata)
    spec = importlib.util.spec_from_file_location(name, ROOT / "phantom.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


for legacy in ("SteamPhantom", "SteamPresence"):
    base = Path(tempfile.mkdtemp())
    (base / legacy).mkdir()
    (base / legacy / "games.json").write_text("[]")
    mod = load("phantom_" + legacy, base)
    assert (base / "Phantom" / "games.json").exists(), legacy
    assert not (base / legacy).exists(), legacy
    assert mod.DATA_DIR == base / "Phantom"
print("migration OK")
