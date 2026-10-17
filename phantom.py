"""
Phantom
-------------
Tiny windows with game banners that report an "In-Game" status to Steam.
Requires: Windows, Steam running and signed in, steam_api64.dll next to the app.

    pip install customtkinter pillow
    python phantom.py
"""
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from translations import LANGUAGES, STRINGS

VERSION = "1.5.0"
AUTHOR = "LayEyzie"
GITHUB_REPO = "LayEyzie/phantom"  # "owner/name" - used by the update check

APPDATA_BASE = Path(os.environ.get("APPDATA", str(Path.home())))
DATA_DIR = APPDATA_BASE / "Phantom"
for _old in ("SteamPhantom", "SteamPresence"):  # earlier names of the app - keep user data
    _old_dir = APPDATA_BASE / _old
    if _old_dir.exists() and not DATA_DIR.exists():
        try:
            _old_dir.rename(DATA_DIR)
        except OSError:
            pass
CACHE_DIR = DATA_DIR / "cache"
CONFIG = DATA_DIR / "games.json"
SETTINGS = DATA_DIR / "settings.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

BANNER_URLS = (
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{id}/header.jpg",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{id}/header.jpg",
)
CREATE_NO_WINDOW = 0x08000000


def resource_path(name: str) -> Path:
    """Path to a bundled resource (works from source and from a PyInstaller exe)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


ICON = resource_path("icon.ico")


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------- data

def load_games() -> list:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return [g for g in data if "id" in g]
    except Exception:
        return []


def save_games(games: list) -> None:
    CONFIG.write_text(json.dumps(games, ensure_ascii=False, indent=2), encoding="utf-8")


DEFAULT_SETTINGS = {
    "run_at_startup": False,
    "autostart_games": False,
        "delay_seconds": 0,
    "hide_uninstalled": True,
    "close_to_tray": True,
    "show_windows": True,
    "watchdog": True,
    "timer_enabled": False,
    "timer_hours": 8,
    "check_updates": True,
    "language": "auto",
    "dll_sha256": "",
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Phantom"
OLD_RUN_NAMES = ("SteamPhantom", "SteamPresence")


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    if "delay_enabled" in saved:  # old format had a separate on/off switch for the delay
        if not saved.pop("delay_enabled"):
            saved["delay_seconds"] = 0
    s.update(saved)
    return s


def save_settings(s: dict) -> None:
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{Path(__file__).resolve()}" --autostart'


def set_startup(enabled: bool) -> bool:
    """Adds/removes the HKCU\\...\\Run entry (no admin rights needed)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            for old_name in OLD_RUN_NAMES:  # drop entries left by earlier app names
                try:
                    winreg.DeleteValue(k, old_name)
                except FileNotFoundError:
                    pass
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


IGNORED_APPIDS = {228980}  # Steamworks Common Redistributables


def find_steam_path():
    """Returns the Steam install folder or None."""
    try:
        import winreg
        for hive, sub, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    p = Path(winreg.QueryValueEx(k, val)[0])
                    if (p / "steamapps").exists():
                        return p
            except OSError:
                continue
    except ImportError:
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base and (Path(base) / "Steam" / "steamapps").exists():
            return Path(base) / "Steam"
    return None


def steam_library_dirs(steam: Path) -> list:
    dirs = [steam / "steamapps"]
    try:
        text = (steam / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\")) / "steamapps"
            if p.exists() and p not in dirs:
                dirs.append(p)
    except OSError:
        pass
    return dirs


def scan_installed():
    """{app_id: name} of games installed via Steam (None = Steam not found)."""
    steam = find_steam_path()
    if steam is None:
        return None
    result = {}
    for lib in steam_library_dirs(steam):
        try:
            files = list(lib.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            def field(key):
                m = re.search(r'"%s"\s+"([^"]*)"' % key, text, re.I)
                return m.group(1) if m else None

            appid, name, flags = field("appid"), field("name"), field("StateFlags")
            if not (appid and appid.isdigit() and name):
                continue
            try:
                if not (int(flags or 0) & 4):  # 4 = FullyInstalled
                    continue
            except ValueError:
                continue
            if int(appid) in IGNORED_APPIDS or name.startswith(
                    ("Steamworks Common", "Proton", "Steam Linux Runtime")):
                continue
            result[int(appid)] = name
    return result


def banner_path(appid: int) -> Path:
    return CACHE_DIR / f"{appid}.jpg"


def fetch_banner(appid: int):
    p = banner_path(appid)
    if p.exists() and p.stat().st_size > 0:
        return p
    for url in BANNER_URLS:
        try:
            req = urllib.request.Request(url.format(id=appid), headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read()
            if data:
                p.write_bytes(data)
                return p
        except Exception:
            continue
    return None


def fetch_name(appid: int):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        entry = data.get(str(appid), {})
        if entry.get("success"):
            return entry["data"]["name"]
    except Exception:
        pass
    return None


LOG = DATA_DIR / "watchdog.log"


def log(msg: str) -> None:
    """Small diagnostic log: %APPDATA%\\Phantom\\watchdog.log"""
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.write_text("", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


def steam_running():
    """True / False, or None when it can't be determined (Steam's own registry pid)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as k:
            pid = winreg.QueryValueEx(k, "pid")[0]
        if not pid:
            return False
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return None


# ---------------------------------------------------------------- language, groups, helpers

LANG = "en"
LANG_IDS = {0x09: "en", 0x15: "pl", 0x07: "de", 0x0A: "es",
            0x0C: "fr", 0x16: "pt", 0x19: "ru", 0x04: "zh"}


def detect_language() -> str:
    """Windows UI language mapped to a supported translation (English by default)."""
    try:
        return LANG_IDS.get(ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF, "en")
    except Exception:
        return "en"


def set_language(code: str) -> None:
    global LANG
    LANG = code if code in STRINGS else "en"


def t(key: str, **kw) -> str:
    """Translated UI text; falls back to English."""
    text = STRINGS.get(LANG, {}).get(key) or STRINGS["en"][key]
    return text.format(**kw) if kw else text


GROUPS = DATA_DIR / "groups.json"


def load_groups() -> list:
    try:
        data = json.loads(GROUPS.read_text(encoding="utf-8"))
        return [g for g in data if isinstance(g, dict) and "name" in g]
    except Exception:
        return []


def save_groups(groups: list) -> None:
    GROUPS.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def parse_version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def fetch_latest_release():
    """(version, url) of a newer GitHub release, or None."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": "Phantom", "Accept": "application/vnd.github+json"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        tag = data.get("tag_name", "")
        if parse_version(tag) > parse_version(VERSION):
            return tag.lstrip("vV"), data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- Steam API file

DLL_NAME = "steam_api64.dll"


def find_dll():
    """steam_api64.dll next to the app (legacy) or in the app's data folder."""
    for p in (base_dir() / DLL_NAME, DATA_DIR / DLL_NAME):
        if p.exists():
            return p
    return None


def dll_capable(path: Path) -> bool:
    """Cheap check (without loading it) that the DLL exports the functions we need."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return (b"SteamAPI_Init" in data
            and b"SteamAPI_SteamApps_v" in data
            and b"SteamAPI_ISteamApps_BIsSubscribedApp" in data)


def dll_signed_by_valve(path: Path):
    """True / False, or None when the check couldn't be performed."""
    ps = ("$s = Get-AuthenticodeSignature -LiteralPath '%s'; "
          "if ($s.Status -eq 'Valid' -and $s.SignerCertificate) "
          "{ $s.SignerCertificate.Subject } else { 'INVALID' }") % str(path).replace("'", "''")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    return "valve" in out.lower() and "INVALID" not in out


def find_dll_candidates(limit: int = 40) -> list:
    """steam_api64.dll copies inside installed Steam games, newest first."""
    steam = find_steam_path()
    if steam is None:
        return []
    found = []
    for lib in steam_library_dirs(steam):
        common = lib / "common"
        if not common.is_dir():
            continue
        for root, dirs, files in os.walk(common):
            if len(Path(root).relative_to(common).parts) >= 4:
                dirs[:] = []  # don't dig deeper than a few levels
            for f in files:
                if f.lower() == DLL_NAME:
                    found.append(Path(root) / f)
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0
    return sorted(found, key=mtime, reverse=True)


def install_dll(src: Path) -> Path:
    dst = DATA_DIR / DLL_NAME
    tmp = dst.with_suffix(".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return dst


def auto_install_dll():
    """Finds a valid, Valve-signed copy in the user's games. Returns (ok, message)."""
    candidates = find_dll_candidates()
    if not candidates:
        return False, t("dll_none_found")
    rejected = set()
    for p in candidates:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size in rejected:  # same size = almost certainly the build we already rejected
            continue
        if not dll_capable(p):
            continue
        if dll_signed_by_valve(p) is not True:
            rejected.add(size)
            continue
        try:
            install_dll(p)
        except OSError as e:
            return False, t("dll_copy_fail", e=e)
        parts = p.parts
        name = parts[parts.index("common") + 1] if "common" in parts else p.parent.name
        return True, t("imported_from", name=name)
    return False, t("dll_none_valid")


def pid_alive(pid: int) -> bool:
    """True while the process with this pid is running (True when it can't be determined)."""
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return True


def kill_tree(proc):
    """Ends a helper together with its children - a onefile exe runs the real app in a child
    process, so terminate() alone would leave the game window behind."""
    try:
        return subprocess.Popen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        return None


def find_stale_helpers(rows, frozen: bool) -> list:
    """Pids of helper processes (--idle) whose main app is gone. rows: [(pid, command line)]."""
    stale = []
    for pid, cmd in rows:
        if "--idle" not in cmd:
            continue
        if not frozen and "phantom.py" not in cmd:
            continue
        found = re.search(r"--parent\s+(\d+)", cmd)
        if not found or not pid_alive(int(found.group(1))):  # no --parent = an old version
            stale.append(pid)
    return stale


def cleanup_stale_helpers() -> int:
    """Closes game windows left behind by an earlier run (crash, kill, older version)."""
    names = sorted({Path(sys.executable).name, "SteamPhantom.exe"})  # also the previous exe name
    flt = " OR ".join("Name='%s'" % n.replace("'", "''") for n in names)
    ps = ('Get-CimInstance Win32_Process -Filter "%s" | '
          'ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }' % flt)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return 0
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.partition("|")
        if pid.strip().isdigit():
            rows.append((int(pid), cmd))
    stale = [p for p in find_stale_helpers(rows, getattr(sys, "frozen", False))
             if p != os.getpid()]
    for pid in stale:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
    if stale:
        log(f"closed {len(stale)} leftover game window process(es): {stale}")
    return len(stale)


SHOW_FLAG = DATA_DIR / "show.flag"
_mutex = None


def acquire_single_instance(wait_seconds: float = 0) -> bool:
    """True if this is the only running instance (named Windows mutex)."""
    global _mutex
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    except Exception:
        return True  # not Windows
    deadline = time.time() + wait_seconds
    while True:
        handle = kernel32.CreateMutexW(None, False, "Local\\PhantomSingleInstance")
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if handle and not already:
            _mutex = handle
            return True
        if handle:
            kernel32.CloseHandle(handle)
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def request_show_existing() -> None:
    """Asks the already running instance to bring its window to the front."""
    try:
        SHOW_FLAG.write_text("1")
    except OSError:
        pass


# ---------------------------------------------------------------- helper process

def emit(msg: str) -> None:
    """Write straight to fd 1 - also works in a windowed exe (sys.stdout is None)."""
    try:
        os.write(1, (msg + "\n").encode("ascii"))
    except OSError:
        pass


def run_idle(appid: int, name: str, minimized: bool = False, parent=None,
             headless: bool = False) -> None:
    """One process per game (Steamworks allows a single App ID per process)."""
    os.environ["SteamAppId"] = str(appid)
    os.environ["SteamGameId"] = str(appid)

    dll_path = find_dll()
    if dll_path is None:
        emit("NO_DLL")
        sys.exit(2)
    os.chdir(dll_path.parent)

    try:
        dll = ctypes.CDLL(str(dll_path))
        dll.SteamAPI_Init.restype = ctypes.c_bool
        if not dll.SteamAPI_Init():
            emit("INIT_FAIL")
            sys.exit(3)
    except OSError:
        emit("INIT_FAIL")
        sys.exit(3)

    # Ownership check: ISteamApps::BIsSubscribedApp
    owned = None
    for ver in range(10, 1, -1):
        getter = getattr(dll, f"SteamAPI_SteamApps_v{ver:03d}", None)
        if getter is None:
            continue
        try:
            getter.restype = ctypes.c_void_p
            ptr = getter()
            check = dll.SteamAPI_ISteamApps_BIsSubscribedApp
            check.restype = ctypes.c_bool
            check.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            owned = bool(check(ptr, appid))
            break
        except Exception:
            continue

    if owned is None:
        dll.SteamAPI_Shutdown()
        emit("NOT_VERIFIED")
        sys.exit(4)
    if not owned:
        dll.SteamAPI_Shutdown()
        emit("NOT_OWNED")
        sys.exit(5)

    emit("OK")

    is_running = getattr(dll, "SteamAPI_IsSteamRunning", None)
    if is_running is not None:
        is_running.restype = ctypes.c_bool
    exit_code = [0]

    if headless:  # no window at all: no Tk, no Pillow - the lowest memory use
        while True:
            if parent and not pid_alive(parent):
                break
            if is_running is not None and not is_running():
                exit_code[0] = 10  # Steam was closed - the main app starts us again later
                break
            dll.SteamAPI_RunCallbacks()
            time.sleep(5)
        dll.SteamAPI_Shutdown()
        sys.exit(exit_code[0])

    import tkinter as tk
    from PIL import Image, ImageTk

    bg = "#1b1b1f"
    root = tk.Tk()
    root.title(name or str(appid))
    root.configure(bg=bg)
    root.resizable(False, False)
    try:
        root.iconbitmap(str(ICON))
    except Exception:
        pass

    path = fetch_banner(appid)
    if path:
        img = Image.open(path)
        img.thumbnail((300, 140))
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(root, image=photo, bd=0, bg=bg)
        lbl.image = photo
        lbl.pack()
    tk.Label(root, text=name or str(appid), fg="white", bg=bg,
             font=("Segoe UI", 10, "bold")).pack(padx=12, pady=8)

    def tick():
        if parent and not pid_alive(parent):  # the main app is gone - don't linger as an orphan
            root.destroy()
            return
        if is_running is not None and not is_running():
            exit_code[0] = 10  # Steam was closed - the main app starts us again later
            root.destroy()
            return
        dll.SteamAPI_RunCallbacks()
        root.after(5000, tick)

    def close():
        if is_running is not None and not is_running():
            exit_code[0] = 10  # window closed because Steam went away
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    tick()
    if exit_code[0] == 0 and root.winfo_exists():
        if minimized:
            root.iconify()
        root.mainloop()
    dll.SteamAPI_Shutdown()
    sys.exit(exit_code[0])


# ---------------------------------------------------------------- GUI

def run_gui() -> None:
    import customtkinter as ctk
    from PIL import Image, ImageDraw
    try:
        import pystray
    except Exception:
        pystray = None

    first_settings = load_settings()
    set_language(detect_language() if first_settings["language"] == "auto"
                 else first_settings["language"])

    try:  # own taskbar identity (shows the ghost icon when run from source)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Phantom")
    except Exception:
        pass

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    IMG_SIZE = (138, 65)
    GREEN = "#3ddc84"
    RED = "#ff6b6b"
    ORANGE = "#e6a23c"
    CARD_BORDER = ("gray85", "gray24")

    MESSAGES = {
        "OK": (t("in_game"), GREEN),
        "NO_DLL": (t("msg_no_dll"), RED),
        "INIT_FAIL": (t("msg_init_fail"), RED),
        "NOT_OWNED": (t("msg_not_owned"), RED),
        "NOT_VERIFIED": (t("msg_not_verified"), RED),
    }

    def bw(*texts, minw=88):
        """Button width that fits the longest of the (translated) texts."""
        return max(minw, 9 * max(len(x) for x in texts) + 28)

    class Elide:
        """Keeps a label's text truncated with an ellipsis to its container's width."""

        def __init__(self, container, label, font, text=""):
            self.container, self.label, self.font, self.full = container, label, font, text
            container.bind("<Configure>", lambda e: self.fit(), add=True)

        def set(self, text, **kw):
            self.full = text
            if kw:
                self.label.configure(**kw)
            self.fit()

        def fit(self):
            width = self.container.winfo_width() - 8
            text = self.full
            if width > 1 and self.font.measure(text) > width:
                while text and self.font.measure(text + "…") > width:
                    text = text[:-1]
                text = text.rstrip() + "…"
            if self.label.cget("text") != text:
                self.label.configure(text=text)

    def rounded_image(path, size, radius=10):
        """CTkImage with rounded corners (2x source for crisp rendering)."""
        w, h = size[0] * 2, size[1] * 2
        if path is not None:
            im = Image.open(path).convert("RGBA").resize((w, h), Image.LANCZOS)
        else:
            im = Image.new("RGBA", (w, h), (52, 52, 58, 255))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius * 2, fill=255)
        im.putalpha(mask)
        return ctk.CTkImage(light_image=im, dark_image=im, size=size)

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("Phantom")
            height = 720
            try:  # keep the window on screen on small or high-DPI displays
                scaling = self._get_window_scaling()
                height = max(520, min(720, int(self.winfo_screenheight() / scaling) - 90))
            except Exception:
                pass
            self.geometry(f"600x{height}")
            self.resizable(False, False)  # fixed size: resizing made the card layout lag
            try:
                SHOW_FLAG.unlink()
            except OSError:
                pass

            self.games = load_games()
            self.groups = load_groups()
            self.procs = {}
            self.cards = {}
            self.failed = set()
            self.q = queue.Queue()

            self.settings = load_settings()
            self.auto_mode = False
            self.retries = {}
            self.autostart = "--autostart" in sys.argv
            self.installed = None
            self.hidden = set()
            self.lib_rows = {}
            self.lib_data = {}
            self.steam_found = True
            self.dll_busy = False
            self.tray = None
            self.closing = False
            self.wanted = set()
            self.retry_jobs = {}
            self.close_times = []
            self.killers = []
            self.started_at = {}
            self.last_up = {}
            self.group_cards = {}
            self.update_url = None
            self.lib_pending = []
            self.lib_building = False
            self.img_cache = {}
            self.no_banner = set()
            self.fetching = set()
            self.dl_q = queue.Queue()
            self.placeholder = rounded_image(None, IMG_SIZE)
            for _ in range(3):
                threading.Thread(target=self.dl_worker, daemon=True).start()
            threading.Thread(target=cleanup_stale_helpers, daemon=True).start()

            head = ctk.CTkFrame(self, fg_color="transparent")
            head.pack(fill="x", padx=22, pady=(22, 8))
            ctk.CTkLabel(head, text="Phantom",
                         font=ctk.CTkFont(size=26, weight="bold")).pack(anchor="w")
            ctk.CTkLabel(head, text=t("subtitle"), text_color="gray60").pack(anchor="w")

            self.tab_library_name = t("tab_library")
            self.tabs = ctk.CTkTabview(self, corner_radius=14, command=self.on_tab_change)
            self.tabs.pack(fill="both", expand=True, padx=14, pady=(0, 14))
            games_tab = self.tabs.add(t("tab_games"))
            library_tab = self.tabs.add(self.tab_library_name)
            groups_tab = self.tabs.add(t("tab_groups"))
            settings_tab = self.tabs.add("\u2699  " + t("tab_settings"))
            try:
                self.tabs._segmented_button.configure(
                    font=ctk.CTkFont(size=14, weight="bold"), height=34)
            except Exception:
                pass

            row = ctk.CTkFrame(games_tab, fg_color="transparent")
            row.pack(fill="x", pady=(6, 0))
            self.entry = ctk.CTkEntry(row, placeholder_text=t("app_id_ph"),
                                      height=42, corner_radius=10)
            self.entry.pack(side="left", fill="x", expand=True)
            self.entry.bind("<Return>", lambda e: self.add_game())
            ctk.CTkButton(row, text=t("add"), width=bw(t("add"), minw=90), height=42,
                          corner_radius=10, command=self.add_game).pack(side="left", padx=(10, 0))

            # warning bar: Steam API file missing
            self.dll_bar = ctk.CTkFrame(games_tab, corner_radius=10,
                                        fg_color=("#fdf0d5", "#3a2f1b"))
            ctk.CTkButton(self.dll_bar, text=t("choose_file"), width=bw(t("choose_file"), minw=100),
                          height=30, corner_radius=8, fg_color="gray30", hover_color="gray25",
                          command=self.choose_dll).pack(side="right", padx=(0, 10), pady=8)
            ctk.CTkButton(self.dll_bar, text=t("auto_detect"), width=bw(t("auto_detect"), minw=100),
                          height=30, corner_radius=8, command=self.setup_dll_async
                          ).pack(side="right", padx=(0, 6), pady=8)
            self.dll_bar_lbl = ctk.CTkLabel(self.dll_bar, text="", anchor="w", justify="left",
                                            wraplength=230, text_color=ORANGE)
            self.dll_bar_lbl.pack(side="left", fill="x", expand=True, padx=12, pady=8)

            # info bar: a newer release is available
            self.update_bar = ctk.CTkFrame(games_tab, corner_radius=10,
                                           fg_color=("#dcecfa", "#1b2f45"))
            ctk.CTkButton(self.update_bar, text="✕", width=30, height=30, corner_radius=8,
                          fg_color="transparent", text_color="gray55",
                          hover_color=("gray80", "gray25"),
                          command=lambda: self.update_bar.pack_forget()
                          ).pack(side="right", padx=(0, 8), pady=8)
            ctk.CTkButton(self.update_bar, text=t("download"), width=bw(t("download"), minw=90),
                          height=30, corner_radius=8,
                          command=lambda: webbrowser.open(self.update_url or "")
                          ).pack(side="right", padx=(0, 6), pady=8)
            self.update_lbl = ctk.CTkLabel(self.update_bar, text="", anchor="w",
                                           text_color=("#1f6aa5", "#6cb6f0"))
            self.update_lbl.pack(side="left", fill="x", expand=True, padx=12, pady=8)

            self.status = ctk.CTkLabel(games_tab, text="", text_color="gray60", anchor="w")
            self.status.pack(fill="x", padx=2, pady=(8, 0))

            self.listf = ctk.CTkScrollableFrame(games_tab, fg_color="transparent")
            self.listf.pack(fill="both", expand=True, pady=6)

            self.hidden_lbl = ctk.CTkLabel(games_tab, text="", anchor="w", text_color="gray60")
            self.hidden_lbl.pack(fill="x", padx=2)

            bottom = ctk.CTkFrame(games_tab, fg_color="transparent")
            bottom.pack(fill="x", pady=(0, 4))
            ctk.CTkButton(bottom, text=t("start_all"), height=38, corner_radius=10,
                          command=self.start_all).pack(side="left", expand=True, fill="x")
            ctk.CTkButton(bottom, text=t("stop_all"), height=38, corner_radius=10,
                          fg_color="gray30", hover_color="gray25",
                          command=self.stop_all).pack(side="left", expand=True, fill="x", padx=(10, 0))

            self.build_library(library_tab)
            self.build_groups(groups_tab)
            self.build_settings(settings_tab)

            for g in self.games:
                self.add_card(g)

            self.start_tray()
            self.refresh_settings_ui()
            self.apply_installed(scan_installed())
            self.check_dll_integrity()
            self.refresh_dll_ui()
            if not find_dll():
                self.after(600, self.setup_dll_async)
            if self.settings["check_updates"]:
                self.after(3000, self.check_update_async)
            self.after(30000, self.periodic_rescan)
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.after(250, self.set_icon)
            self.after(100, self.pump)
            self.after(1000, self.poll_procs)

            # refresh the registry path (e.g. after the exe was moved)
            if self.settings["run_at_startup"]:
                set_startup(True)

            if self.autostart:
                self.after(300, self.hide_on_autostart)
                if (self.settings["run_at_startup"] and self.settings["autostart_games"]
                        and self.games):
                    delay = self.settings["delay_seconds"]
                    self.auto_countdown(int(delay))

            if "--resume" in sys.argv:  # restarted by the language switch: bring games back
                ids = []
                try:
                    f = DATA_DIR / "resume.json"
                    ids = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink()
                except Exception:
                    pass
                if ids:
                    self.after(2500, lambda: self.resume_games(ids))

        # ---- cards
        def get_image(self, appid):
            """Banner for the game (rounded); a placeholder until it is downloaded."""
            if appid in self.img_cache:
                return self.img_cache[appid]
            p = banner_path(appid)
            if p.exists() and p.stat().st_size > 0:
                try:
                    ci = rounded_image(p, IMG_SIZE)
                    self.img_cache[appid] = ci
                    return ci
                except Exception:
                    self.no_banner.add(appid)
            elif appid not in self.no_banner and appid not in self.fetching:
                self.fetching.add(appid)
                self.dl_q.put(appid)
            return self.placeholder

        def dl_worker(self):
            while True:
                appid = self.dl_q.get()
                fetch_banner(appid)
                self.q.put(("banner", appid, None))

        def apply_banner(self, appid):
            ci = self.get_image(appid)
            c = self.cards.get(appid)
            if c:
                c["img"].configure(image=ci)
            r = self.lib_rows.get(appid)
            if r:
                r["img"].configure(image=ci)

        def make_row_frame(self, parent, appid, full_name):
            """Shared layout of a game row: banner | title + lines | actions column."""
            frame = ctk.CTkFrame(parent, corner_radius=14, border_width=2,
                                 border_color=CARD_BORDER)
            frame.grid_columnconfigure(1, weight=1)
            img = ctk.CTkLabel(frame, text="", image=self.get_image(appid),
                               width=IMG_SIZE[0], height=IMG_SIZE[1])
            img.grid(row=0, column=0, padx=(12, 14), pady=12)

            # fixed height + no propagation: the info column can never stretch the row
            info = ctk.CTkFrame(frame, fg_color="transparent", width=1, height=IMG_SIZE[1])
            info.grid(row=0, column=1, sticky="nsew", pady=12)
            info.pack_propagate(False)
            inner = ctk.CTkFrame(info, fg_color="transparent")
            inner.pack(fill="x", expand=True)  # expand without fill=y -> vertically centered

            title_font = ctk.CTkFont(size=15, weight="bold")
            title = ctk.CTkLabel(inner, text=full_name, anchor="w", font=title_font, height=22)
            title.pack(fill="x")
            title_el = Elide(info, title, title_font, full_name)
            return frame, img, info, inner, title_el

        def add_card(self, game):
            appid = game["id"]
            full_name = game.get("name") or str(appid)
            frame, img, info, inner, _ = self.make_row_frame(self.listf, appid, full_name)
            frame.pack(fill="x", padx=6, pady=6)

            status_font = ctk.CTkFont(size=12)
            idle_text = "● " + t("idle")
            status = ctk.CTkLabel(inner, text=idle_text, anchor="w", text_color="gray60",
                                  font=status_font, height=18)
            status.pack(fill="x")
            ctk.CTkLabel(inner, text=f"ID {appid}", anchor="w", text_color="gray50",
                         font=ctk.CTkFont(size=11), height=16).pack(fill="x")
            status_el = Elide(info, status, status_font, idle_text)

            right = ctk.CTkFrame(frame, fg_color="transparent")
            right.grid(row=0, column=2, padx=(8, 14), pady=12)
            btn = ctk.CTkButton(right, text=t("start"), width=bw(t("start"), t("stop")),
                                height=34, corner_radius=10,
                                font=ctk.CTkFont(size=13, weight="bold"),
                                command=lambda a=appid: self.toggle(a))
            btn.pack(fill="x")
            ctk.CTkButton(right, text="✕", width=28, height=24, corner_radius=8,
                          fg_color="transparent", text_color="gray55",
                          hover_color=("gray80", "gray25"), font=ctk.CTkFont(size=12),
                          command=lambda a=appid: self.remove_game(a)).pack(pady=(6, 0))

            self.cards[appid] = dict(frame=frame, img=img, status=status_el, btn=btn)

        def set_state(self, appid, text, color="gray60"):
            c = self.cards.get(appid)
            if c:
                c["status"].set(f"● {text}", text_color=color)
                c["frame"].configure(border_color=GREEN if color == GREEN else CARD_BORDER)

        # ---- adding / removing
        def add_game(self):
            raw = self.entry.get().strip()
            if not raw.isdigit():
                self.status.configure(text=t("bad_app_id"), text_color=RED)
                return
            appid = int(raw)
            if any(g["id"] == appid for g in self.games):
                self.status.configure(text=t("already_listed"), text_color=RED)
                return
            self.status.configure(text=t("fetching"), text_color="gray60")
            self.entry.delete(0, "end")

            def work():
                name = fetch_name(appid)
                if name:
                    fetch_banner(appid)
                self.q.put(("added", appid, name))
            threading.Thread(target=work, daemon=True).start()

        def remove_game(self, appid):
            self.stop(appid)
            self.games = [g for g in self.games if g["id"] != appid]
            save_games(self.games)
            c = self.cards.pop(appid, None)
            if c:
                c["frame"].destroy()
            self.hidden.discard(appid)
            self.refresh_library_buttons()
            self.apply_visibility()
            self.render_groups()

        # ---- start / stop
        def toggle(self, appid):
            if appid in self.procs or appid in self.wanted:
                self.stop(appid)
            else:
                self.start(appid)

        def set_btn(self, appid, running):
            c = self.cards.get(appid)
            if not c:
                return
            if running:
                c["btn"].configure(text=t("stop"), fg_color="#c0392b", hover_color="#a93226")
            else:
                theme = ctk.ThemeManager.theme["CTkButton"]
                c["btn"].configure(text=t("start"), fg_color=theme["fg_color"],
                                   hover_color=theme["hover_color"])

        def start(self, appid, minimized=False):
            if appid in self.procs:
                return
            if not self.check_dll_integrity():
                self.wanted.discard(appid)
                self.set_btn(appid, False)
                self.set_state(appid, t("dll_changed"), RED)
                return
            name = next((g.get("name", "") for g in self.games if g["id"] == appid), "")
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--idle", str(appid), name]
            else:
                cmd = [sys.executable, str(Path(__file__).resolve()), "--idle", str(appid), name]
            if minimized:
                cmd.append("--minimized")
            if not self.settings["show_windows"]:
                cmd.append("--headless")
            cmd += ["--parent", str(os.getpid())]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                                        creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                self.set_state(appid, t("error_fmt", e=e), RED)
                return
            self.procs[appid] = proc
            self.wanted.add(appid)  # the user wants this game running (watchdog keeps it so)
            self.failed.discard(appid)
            self.set_btn(appid, True)
            self.set_state(appid, t("connecting"))

            def reader():
                line = proc.stdout.readline().strip() if proc.stdout else ""
                self.q.put(("result", appid, line))
            threading.Thread(target=reader, daemon=True).start()

        def stop(self, appid, keep_wanted=False):
            """keep_wanted=True is used internally when a process ended on its own."""
            proc = self.procs.pop(appid, None)
            if proc and proc.poll() is None:
                self.killers.append(kill_tree(proc))
            self.started_at.pop(appid, None)
            self.last_up.pop(appid, None)
            if not keep_wanted:
                self.wanted.discard(appid)
                self.retries.pop(appid, None)
                job = self.retry_jobs.pop(appid, None)
                if job:
                    self.after_cancel(job)
            waiting = appid in self.wanted
            self.set_btn(appid, waiting)
            if not keep_wanted:
                self.failed.discard(appid)
                self.set_state(appid, t("idle"))
            elif appid not in self.failed and not waiting:
                self.set_state(appid, t("idle"))

        def start_all(self, auto=False, minimized=None):
            self.auto_mode = auto
            mn = auto if minimized is None else minimized
            for g in self.games:
                if g["id"] not in self.hidden:
                    self.start(g["id"], minimized=mn)

        def stop_all(self):
            for appid in set(self.procs) | self.wanted:
                self.stop(appid)

        def check_closed(self, appid, attempt=0):
            """A game window closed with code 0: the user did it - or Steam shutting down did."""
            if appid not in self.wanted or appid in self.procs:
                return  # stopped or restarted in the meantime
            running = steam_running()
            log(f"{appid}: window closed, steam_running={running}, check #{attempt}")
            if self.settings["watchdog"] and running is False:
                self.failed.add(appid)
                self.set_state(appid, t("waiting_steam"))
                self.schedule_retry(appid, 15000)
            elif self.settings["watchdog"] and running is True and attempt < 8:
                # Steam needs a while to shut down - look again before deciding
                self.after(2000, lambda a=appid, n=attempt + 1: self.check_closed(a, n))
            else:
                self.wanted.discard(appid)
                self.set_btn(appid, False)
                self.set_state(appid, t("idle"))

        # ---- watchdog
        def schedule_retry(self, appid, delay_ms):
            if appid in self.retry_jobs:
                return
            log(f"{appid}: retry in {delay_ms // 1000}s")
            self.retry_jobs[appid] = self.after(delay_ms, lambda a=appid: self.run_retry(a))

        def run_retry(self, appid):
            self.retry_jobs.pop(appid, None)
            ok = (appid in self.wanted and appid in self.cards
                  and appid not in self.procs and appid not in self.hidden)
            log(f"{appid}: retry now, will start={ok}")
            if ok:
                self.start(appid, minimized=True)

        # ---- groups
        def build_groups(self, parent):
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x", pady=(6, 0))
            self.group_entry = ctk.CTkEntry(top, placeholder_text=t("groups_ph"),
                                            height=42, corner_radius=10)
            self.group_entry.pack(side="left", fill="x", expand=True)
            self.group_entry.bind("<Return>", lambda e: self.create_group())
            ctk.CTkButton(top, text=t("create"), width=bw(t("create"), minw=90), height=42,
                          corner_radius=10, command=self.create_group
                          ).pack(side="left", padx=(10, 0))
            self.group_info = ctk.CTkLabel(parent, text=t("groups_hint"), anchor="w",
                                           justify="left", wraplength=470, text_color="gray60")
            self.group_info.pack(fill="x", padx=2, pady=(8, 0))
            self.groups_list = ctk.CTkScrollableFrame(parent, fg_color="transparent")
            self.groups_list.pack(fill="both", expand=True, pady=6)
            self.render_groups()

        def find_group(self, name):
            return next((g for g in self.groups if g["name"] == name), None)

        def group_ids(self, g):
            existing = {x["id"] for x in self.games}
            return [i for i in g.get("ids", []) if i in existing]

        def group_running(self, g):
            return any(i in self.procs or i in self.wanted for i in self.group_ids(g))

        def render_groups(self):
            for c in self.group_cards.values():
                c["frame"].destroy()
            self.group_cards = {}
            for g in self.groups:
                name = g["name"]
                frame = ctk.CTkFrame(self.groups_list, corner_radius=14, border_width=2,
                                     border_color=CARD_BORDER)
                frame.pack(fill="x", padx=4, pady=5)
                frame.grid_columnconfigure(0, weight=1)
                info = ctk.CTkFrame(frame, fg_color="transparent", width=1, height=44)
                info.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=14)
                info.pack_propagate(False)
                inner = ctk.CTkFrame(info, fg_color="transparent")
                inner.pack(fill="x", expand=True)
                font = ctk.CTkFont(size=15, weight="bold")
                lbl = ctk.CTkLabel(inner, text=name, anchor="w", font=font, height=22)
                lbl.pack(fill="x")
                Elide(info, lbl, font, name)
                n = len(self.group_ids(g))
                ctk.CTkLabel(inner, text=t("group_count", n=n) if n else t("group_empty"),
                             anchor="w", text_color="gray60", font=ctk.CTkFont(size=12),
                             height=18).pack(fill="x")

                btns = ctk.CTkFrame(frame, fg_color="transparent")
                btns.grid(row=0, column=1, padx=(0, 14), pady=14)
                start_btn = ctk.CTkButton(btns, text=t("start"), width=bw(t("start"), t("stop"), minw=72),
                                          height=32, corner_radius=8,
                                          command=lambda nm=name: self.toggle_group(nm))
                start_btn.pack(side="left")
                ctk.CTkButton(btns, text=t("edit"), width=bw(t("edit"), minw=64), height=32,
                              corner_radius=8, fg_color="gray30", hover_color="gray25",
                              command=lambda nm=name: self.edit_group(nm)
                              ).pack(side="left", padx=(6, 0))
                ctk.CTkButton(btns, text="✕", width=32, height=32, corner_radius=8,
                              fg_color="transparent", text_color="gray55",
                              hover_color=("gray80", "gray25"),
                              command=lambda nm=name: self.delete_group(nm)
                              ).pack(side="left", padx=(6, 0))
                self.group_cards[name] = dict(frame=frame, btn=start_btn, running=None)
            self.refresh_group_buttons()

        def refresh_group_buttons(self):
            theme = ctk.ThemeManager.theme["CTkButton"]
            for g in self.groups:
                c = self.group_cards.get(g["name"])
                if not c:
                    continue
                running = self.group_running(g)
                if c["running"] == running:
                    continue
                c["running"] = running
                if running:
                    c["btn"].configure(text=t("stop"), fg_color="#c0392b", hover_color="#a93226")
                else:
                    c["btn"].configure(text=t("start"), fg_color=theme["fg_color"],
                                       hover_color=theme["hover_color"])

        def toggle_group(self, name):
            g = self.find_group(name)
            if not g:
                return
            ids = self.group_ids(g)
            if self.group_running(g):
                for appid in ids:
                    self.stop(appid)
            else:
                for appid in ids:
                    if appid not in self.hidden:
                        self.start(appid)
            self.refresh_group_buttons()

        def create_group(self):
            name = self.group_entry.get().strip()
            if not name:
                self.group_info.configure(text=t("group_name_empty"), text_color=RED)
                return
            if any(g["name"].lower() == name.lower() for g in self.groups):
                self.group_info.configure(text=t("group_exists"), text_color=RED)
                return
            self.groups.append({"name": name, "ids": []})
            save_groups(self.groups)
            self.group_entry.delete(0, "end")
            self.group_info.configure(text=t("groups_hint"), text_color="gray60")
            self.render_groups()
            self.edit_group(name)

        def delete_group(self, name):
            self.groups = [g for g in self.groups if g["name"] != name]
            save_groups(self.groups)
            self.render_groups()

        def edit_group(self, name):
            g = self.find_group(name)
            if not g:
                return
            if not self.games:
                self.group_info.configure(text=t("no_games_yet"), text_color=ORANGE)
                return
            dlg = ctk.CTkToplevel(self)
            dlg.title(t("edit_group", name=name))
            dlg.geometry("420x520")
            dlg.resizable(False, False)
            dlg.after(250, lambda: dlg.iconbitmap(str(ICON)))
            dlg.after(100, dlg.grab_set)
            ctk.CTkLabel(dlg, text=t("edit_group", name=name),
                         font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=20, pady=(16, 8))
            lst = ctk.CTkScrollableFrame(dlg)
            lst.pack(fill="both", expand=True, padx=16, pady=4)
            boxes = {}
            for game in self.games:
                cb = ctk.CTkCheckBox(lst, text=game.get("name") or str(game["id"]))
                if game["id"] in g.get("ids", []):
                    cb.select()
                cb.pack(anchor="w", pady=4, padx=6)
                boxes[game["id"]] = cb

            def save():
                g["ids"] = [i for i, cb in boxes.items() if cb.get()]
                save_groups(self.groups)
                dlg.destroy()
                self.render_groups()

            row = ctk.CTkFrame(dlg, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=14)
            ctk.CTkButton(row, text=t("save"), width=bw(t("save")), command=save).pack(side="right")
            ctk.CTkButton(row, text=t("cancel"), width=bw(t("cancel")), fg_color="gray30",
                          hover_color="gray25", command=dlg.destroy).pack(side="right", padx=(0, 8))

        # ---- settings
        def build_settings(self, parent):
            parent = ctk.CTkScrollableFrame(parent, fg_color="transparent")
            parent.pack(fill="both", expand=True)
            s = self.settings

            def section(title):
                ctk.CTkLabel(parent, text=title.upper(), anchor="w", text_color="gray55",
                             font=ctk.CTkFont(size=12, weight="bold")
                             ).pack(fill="x", padx=10, pady=(18, 0))

            def card(title, desc, hint=False):
                c = ctk.CTkFrame(parent, corner_radius=14)
                c.pack(fill="x", padx=4, pady=6)
                top = ctk.CTkFrame(c, fg_color="transparent")
                top.pack(fill="x", padx=16, pady=(14, 0))
                ctk.CTkLabel(top, text=title, anchor="w",
                             font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
                ctk.CTkLabel(c, text=desc, anchor="w", justify="left", wraplength=420,
                             text_color="gray60").pack(fill="x", padx=16, pady=(2, 0))
                h = None
                if hint:
                    h = ctk.CTkLabel(c, text="", height=1, anchor="w", justify="left",
                                     wraplength=420, text_color=ORANGE)
                    h.pack(fill="x", padx=16)
                return c, top, h

            def switch_card(title, desc, command, key, hint=False, pad=True):
                c, top, h = card(title, desc, hint)
                sw = ctk.CTkSwitch(top, text="", width=46, command=command)
                sw.pack(side="right")
                if s[key]:
                    sw.select()
                if pad:
                    ctk.CTkFrame(c, height=12, fg_color="transparent").pack()
                return c, sw, h

            def number_row(c, label, value, unit, callback, indent=16):
                row = ctk.CTkFrame(c, fg_color="transparent")
                row.pack(fill="x", padx=(indent, 16), pady=(8, 14))
                ctk.CTkLabel(row, text=label, text_color="gray60").pack(side="left")
                var = ctk.StringVar(value=str(value))
                entry = ctk.CTkEntry(row, width=70, height=32, justify="center", textvariable=var)
                entry.pack(side="left", padx=8)
                ctk.CTkLabel(row, text=unit, text_color="gray60").pack(side="left")
                var.trace_add("write", callback)
                return var, entry

            # ---- Startup: one card, options nested under "Launch at system startup"
            section(t("sec_startup"))
            c1, self.sw_startup, _ = switch_card(t("startup_title"), t("startup_desc"),
                                                 self.on_startup_toggle, "run_at_startup",
                                                 pad=False)
            sub = ctk.CTkFrame(c1, fg_color="transparent")
            sub.pack(fill="x", padx=(36, 16), pady=(12, 0))
            ctk.CTkLabel(sub, text=t("startup_games"), anchor="w").pack(side="left")
            self.sw_auto = ctk.CTkSwitch(sub, text="", width=46, command=self.on_auto_toggle)
            self.sw_auto.pack(side="right")
            if s["autostart_games"]:
                self.sw_auto.select()
            self.delay_var, self.delay_entry = number_row(
                c1, t("startup_wait"), s["delay_seconds"], t("delay_unit"),
                self.on_delay_change, indent=36)

            # ---- Behavior
            section(t("sec_behavior"))
            _, self.sw_tray, self.tray_hint = switch_card(
                t("tray_title"), t("tray_desc"), self.on_tray_toggle, "close_to_tray", hint=True)
            _, self.sw_windows, _ = switch_card(t("windows_title"), t("windows_desc"),
                                                self.on_windows_toggle, "show_windows")
            _, self.sw_watch, _ = switch_card(t("watch_title"), t("watch_desc"),
                                              self.on_watch_toggle, "watchdog")
            c4, self.sw_timer, _ = switch_card(t("timer_title"), t("timer_desc"),
                                               self.on_timer_toggle, "timer_enabled", pad=False)
            self.timer_var, self.timer_entry = number_row(
                c4, t("timer_after"), s["timer_hours"], t("hours"), self.on_timer_change)
            _, self.sw_hide, _ = switch_card(t("hide_title"), t("hide_desc"),
                                             self.on_hide_toggle, "hide_uninstalled")

            # ---- Steam
            section(t("sec_steam"))
            c0, _, _ = card(t("set_dll_title"), t("set_dll_desc"))
            self.dll_status = ctk.CTkLabel(c0, text="", anchor="w", justify="left",
                                           wraplength=420, text_color="gray60")
            self.dll_status.pack(fill="x", padx=16, pady=(6, 0))
            brow = ctk.CTkFrame(c0, fg_color="transparent")
            brow.pack(fill="x", padx=16, pady=(8, 14))
            ctk.CTkButton(brow, text=t("auto_detect"), width=bw(t("auto_detect"), minw=110),
                          height=32, corner_radius=8, command=self.setup_dll_async
                          ).pack(side="left")
            ctk.CTkButton(brow, text=t("choose_file"), width=bw(t("choose_file"), minw=110),
                          height=32, corner_radius=8, fg_color="gray30", hover_color="gray25",
                          command=self.choose_dll).pack(side="left", padx=(8, 0))

            # ---- General
            section(t("sec_general"))
            c9, top9, _ = card(t("lang_title"), t("lang_desc"))
            self.lang_codes = ["auto"] + list(LANGUAGES)
            self.lang_names = [t("lang_auto")] + [LANGUAGES[c] for c in LANGUAGES]
            self.lang_menu = ctk.CTkOptionMenu(top9, values=self.lang_names, width=180,
                                               command=self.on_language_change)
            cur = s["language"] if s["language"] in self.lang_codes else "auto"
            self.lang_menu.set(self.lang_names[self.lang_codes.index(cur)])
            self.lang_menu.pack(side="right")
            ctk.CTkFrame(c9, height=12, fg_color="transparent").pack()
            _, self.sw_updates, _ = switch_card(t("updates_title"), t("updates_desc"),
                                                self.on_updates_toggle, "check_updates")

            # ---- Data
            section(t("sec_data"))
            c10, _, _ = card(t("list_title"), t("list_desc"))
            lrow = ctk.CTkFrame(c10, fg_color="transparent")
            lrow.pack(fill="x", padx=16, pady=(8, 14))
            ctk.CTkButton(lrow, text=t("export_btn"), width=bw(t("export_btn"), minw=110),
                          height=32, corner_radius=8, command=self.export_games).pack(side="left")
            ctk.CTkButton(lrow, text=t("import_btn"), width=bw(t("import_btn"), minw=110),
                          height=32, corner_radius=8, fg_color="gray30", hover_color="gray25",
                          command=self.import_games).pack(side="left", padx=(8, 0))
            ctk.CTkButton(c10, text=t("open_folder"), width=bw(t("open_folder"), minw=110),
                          height=32, corner_radius=8, fg_color="gray30", hover_color="gray25",
                          command=self.open_data_folder).pack(anchor="w", padx=16, pady=(0, 14))

            self.set_msg = ctk.CTkLabel(parent, text="", anchor="w", justify="left",
                                        wraplength=440, text_color="gray60")
            self.set_msg.pack(fill="x", padx=8, pady=(4, 0))

            footer = ctk.CTkFrame(parent, fg_color="transparent")
            footer.pack(fill="x", pady=(20, 10))
            try:
                logo = Image.open(ICON).convert("RGBA")
                self.footer_img = ctk.CTkImage(light_image=logo, dark_image=logo, size=(52, 52))
                ctk.CTkLabel(footer, text="", image=self.footer_img).pack()
            except Exception:
                pass
            ctk.CTkLabel(footer, text="Phantom",
                         font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(4, 0))
            ctk.CTkLabel(footer, text=t("footer_fmt", v=VERSION, author=AUTHOR),
                         text_color="gray55", font=ctk.CTkFont(size=12)).pack(pady=(2, 0))

            self.refresh_settings_ui()

        def refresh_settings_ui(self):
            s = self.settings
            startup_on = s["run_at_startup"]
            auto_on = startup_on and s["autostart_games"]
            self.sw_auto.configure(state="normal" if startup_on else "disabled")
            self.delay_entry.configure(state="normal" if auto_on else "disabled")
            self.timer_entry.configure(state="normal" if s["timer_enabled"] else "disabled")
            self.set_hint(self.tray_hint, "" if self.tray else t("tray_unavail"))
            self.sw_tray.configure(state="normal" if self.tray else "disabled")

        @staticmethod
        def set_hint(label, text):
            label.configure(text=text, height=22 if text else 1)

        def save_setting(self, key, value):
            self.settings[key] = value
            save_settings(self.settings)

        def on_startup_toggle(self):
            want = bool(self.sw_startup.get())
            if not set_startup(want):
                (self.sw_startup.deselect if want else self.sw_startup.select)()
                self.set_msg.configure(text=t("startup_fail"), text_color=RED)
                return
            self.save_setting("run_at_startup", want)
            self.set_msg.configure(text=t("startup_on") if want else t("startup_off"),
                                   text_color="gray60")
            self.refresh_settings_ui()

        def on_auto_toggle(self):
            self.save_setting("autostart_games", bool(self.sw_auto.get()))
            self.refresh_settings_ui()

        def on_delay_change(self, *_):
            raw = self.delay_var.get()
            v = "".join(ch for ch in raw if ch.isdigit())[:4]
            if v != raw:
                self.delay_var.set(v)
                return
            if v:
                self.save_setting("delay_seconds", min(int(v), 3600))

        def on_timer_toggle(self):
            self.save_setting("timer_enabled", bool(self.sw_timer.get()))
            self.refresh_settings_ui()

        def on_timer_change(self, *_):
            raw = self.timer_var.get()
            v = "".join(ch for ch in raw if ch.isdigit())[:3]
            if v != raw:
                self.timer_var.set(v)
                return
            if v and int(v) > 0:
                self.save_setting("timer_hours", min(int(v), 720))

        def on_tray_toggle(self):
            self.save_setting("close_to_tray", bool(self.sw_tray.get()))

        def on_windows_toggle(self):
            self.save_setting("show_windows", bool(self.sw_windows.get()))

        def open_data_folder(self):
            try:
                os.startfile(str(DATA_DIR))
            except AttributeError:  # not Windows
                webbrowser.open(DATA_DIR.as_uri())
            except OSError:
                pass

        def on_watch_toggle(self):
            self.save_setting("watchdog", bool(self.sw_watch.get()))

        def on_updates_toggle(self):
            self.save_setting("check_updates", bool(self.sw_updates.get()))

        def on_hide_toggle(self):
            self.save_setting("hide_uninstalled", bool(self.sw_hide.get()))
            self.apply_visibility(force=True)

        def on_language_change(self, name):
            code = self.lang_codes[self.lang_names.index(name)]
            if code == self.settings["language"]:
                return
            self.save_setting("language", code)
            from tkinter import messagebox
            set_language(detect_language() if code == "auto" else code)  # ask in the new language
            if messagebox.askyesno(t("restart_title"), t("restart_msg")):
                self.restart_app()

        def wait_killers(self):
            """Let the taskkill helpers finish so no game window outlives the app."""
            for k in self.killers:
                try:
                    k.wait(timeout=3)
                except Exception:
                    pass
            self.killers = []

        def restart_app(self):
            try:
                (DATA_DIR / "resume.json").write_text(json.dumps(sorted(self.wanted)),
                                                      encoding="utf-8")
            except OSError:
                pass
            self.stop_all()
            self.wait_killers()
            if getattr(sys, "frozen", False):
                cmd = [sys.executable]
            else:
                cmd = [sys.executable, str(Path(__file__).resolve())]
            env = dict(os.environ)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # let the new onefile exe unpack itself
            try:
                subprocess.Popen(cmd + ["--resume"], env=env, close_fds=True,
                                 creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
            except Exception:
                return
            self.quit_app()

        def resume_games(self, ids):
            for appid in ids:
                if appid in self.cards and appid not in self.hidden:
                    self.start(appid, minimized=True)

        def auto_countdown(self, remaining):
            if remaining > 0:
                self.status.configure(text=t("countdown", n=remaining), text_color="gray60")
                self.after(1000, lambda: self.auto_countdown(remaining - 1))
            else:
                self.status.configure(text="")
                self.start_all(auto=True)

        # ---- export / import
        def export_games(self):
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                title=t("export_title"), defaultextension=".json",
                initialfile="phantom-games.json",
                filetypes=[(t("filter_json"), "*.json")])
            if not path:
                return
            try:
                Path(path).write_text(
                    json.dumps({"games": self.games, "groups": self.groups},
                               ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                self.set_msg.configure(text=t("error_fmt", e=e), text_color=RED)
                return
            self.set_msg.configure(text=t("exported", n=len(self.games)), text_color=GREEN)

        def import_games(self):
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title=t("import_title"), filetypes=[(t("filter_json"), "*.json")])
            if not path:
                return
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                games = data if isinstance(data, list) else data["games"]
                groups = [] if isinstance(data, list) else data.get("groups", [])
                if not isinstance(games, list):
                    raise ValueError
            except Exception:
                self.set_msg.configure(text=t("import_bad"), text_color=RED)
                return
            existing = {g["id"] for g in self.games}
            added = 0
            for g in games:
                if not (isinstance(g, dict) and isinstance(g.get("id"), int)) or g["id"] in existing:
                    continue
                entry = {"id": g["id"], "name": str(g.get("name") or g["id"])}
                self.games.append(entry)
                existing.add(entry["id"])
                self.add_card(entry)
                added += 1
            names = {x["name"].lower() for x in self.groups}
            for gr in groups:
                if (isinstance(gr, dict) and isinstance(gr.get("name"), str)
                        and isinstance(gr.get("ids"), list) and gr["name"].lower() not in names):
                    self.groups.append({"name": gr["name"],
                                        "ids": [i for i in gr["ids"] if isinstance(i, int)]})
                    names.add(gr["name"].lower())
            save_games(self.games)
            save_groups(self.groups)
            self.apply_visibility(force=True)
            self.refresh_library_buttons()
            self.render_groups()
            self.set_msg.configure(text=t("imported", n=added), text_color=GREEN)

        # ---- Steam API file
        def refresh_dll_ui(self, note=None):
            p = find_dll()
            if p:
                self.dll_bar.pack_forget()
                self.dll_status.configure(text=t("dll_ready", path=p), text_color=GREEN)
            else:
                text = note or t("dll_missing")
                self.dll_status.configure(text=f"✗ {text}", text_color=RED)
                self.dll_bar_lbl.configure(text=text)
                self.dll_bar.pack(fill="x", pady=(8, 0), before=self.status)

        def setup_dll_async(self):
            if self.dll_busy:
                return
            self.dll_busy = True
            self.dll_status.configure(text=t("dll_searching"), text_color="gray60")
            self.dll_bar_lbl.configure(text=t("dll_searching_bar"))
            if not find_dll():
                self.dll_bar.pack(fill="x", pady=(8, 0), before=self.status)

            def work():
                ok, msg = auto_install_dll()
                self.q.put(("dll", int(ok), msg))
            threading.Thread(target=work, daemon=True).start()

        def choose_dll(self):
            from tkinter import filedialog, messagebox
            path = filedialog.askopenfilename(
                title=t("select_dll_title"),
                filetypes=[(t("dll_filter_api"), "steam_api*.dll"), (t("dll_filter_all"), "*.dll")])
            if not path:
                return
            p = Path(path)
            if not dll_capable(p):
                messagebox.showerror("Phantom", t("dll_bad"))
                return
            if dll_signed_by_valve(p) is not True and not messagebox.askyesno(
                    t("unverified_title"), t("unverified_msg"), default="no"):
                return
            try:
                install_dll(p)
            except OSError as e:
                messagebox.showerror("Phantom", t("copy_fail_dialog", e=e))
                return
            self.remember_dll_hash()
            self.refresh_dll_ui()
            self.status.configure(text=t("dll_ready_short"), text_color=GREEN)

        def remember_dll_hash(self):
            p = DATA_DIR / DLL_NAME
            if p.exists():
                self.save_setting("dll_sha256", file_sha256(p))

        def check_dll_integrity(self):
            """True while the app's own copy of the DLL is untouched; otherwise restores it."""
            p = DATA_DIR / DLL_NAME
            if not p.exists():
                return True
            try:
                actual = file_sha256(p)
            except OSError:
                return True
            stored = self.settings.get("dll_sha256", "")
            if not stored:  # first run with this feature: trust what is there now
                self.save_setting("dll_sha256", actual)
                return True
            if actual == stored:
                return True
            log(f"DLL hash mismatch (expected {stored[:12]}, got {actual[:12]}) - restoring")
            try:
                p.unlink()
            except OSError:
                return False
            self.save_setting("dll_sha256", "")
            self.refresh_dll_ui(t("dll_changed"))
            self.setup_dll_async()
            return False

        # ---- updates
        def check_update_async(self):
            def work():
                self.q.put(("update", 0, fetch_latest_release()))
            threading.Thread(target=work, daemon=True).start()

        def show_update(self, info):
            version, url = info
            self.update_url = url
            self.update_lbl.configure(text=t("update_avail", v=version))
            self.update_bar.pack(fill="x", pady=(8, 0), before=self.status)

        # ---- installed games library
        def build_library(self, parent):
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x", pady=(6, 0))
            self.lib_search = ctk.CTkEntry(top, placeholder_text=t("lib_search_ph"),
                                           height=42, corner_radius=10)
            self.lib_search.pack(side="left", fill="x", expand=True)
            self.lib_search.bind("<KeyRelease>", lambda e: self.filter_library())
            ctk.CTkButton(top, text=t("refresh"), width=bw(t("refresh"), minw=90), height=42,
                          corner_radius=10, command=self.rescan_async
                          ).pack(side="left", padx=(10, 0))
            self.lib_info = ctk.CTkLabel(parent, text="", anchor="w", text_color="gray60")
            self.lib_info.pack(fill="x", padx=2, pady=(8, 0))
            self.lib_list = ctk.CTkScrollableFrame(parent, fg_color="transparent")
            self.lib_list.pack(fill="both", expand=True, pady=6)

        def make_lib_row(self, appid, name):
            frame, img, info, inner, _ = self.make_row_frame(self.lib_list, appid, name)
            ctk.CTkLabel(inner, text=f"ID {appid}", anchor="w", text_color="gray50",
                         font=ctk.CTkFont(size=11), height=16).pack(fill="x")
            btn = ctk.CTkButton(frame, text=t("add"), width=bw(t("add"), t("added_btn")),
                                height=34, corner_radius=10,
                                font=ctk.CTkFont(size=13, weight="bold"),
                                command=lambda a=appid, n=name: self.add_from_library(a, n))
            btn.grid(row=0, column=2, padx=(8, 14), pady=12)
            return dict(frame=frame, btn=btn, img=img, text=f"{name.lower()} {appid}")

        def update_library(self, data):
            self.steam_found = data is not None
            data = data or {}
            if data != self.lib_data:
                for r in self.lib_rows.values():
                    r["frame"].destroy()
                self.lib_rows = {}
                self.lib_data = dict(data)
                self.lib_pending = sorted(data.items(), key=lambda kv: kv[1].lower())
                if not self.lib_building:
                    self.lib_building = True
                    self.build_lib_rows()
            else:
                self.update_lib_info()
                self.refresh_library_buttons()

        def build_lib_rows(self):
            """Builds rows in small batches so the window never freezes."""
            batch, self.lib_pending = self.lib_pending[:6], self.lib_pending[6:]
            for appid, name in batch:
                self.lib_rows[appid] = self.make_lib_row(appid, name)
            self.filter_library()
            self.refresh_library_buttons()
            if self.lib_pending:
                self.after(20, self.build_lib_rows)
            else:
                self.lib_building = False

        def filter_library(self):
            q = self.lib_search.get().strip().lower()
            for r in self.lib_rows.values():
                r["frame"].pack_forget()
            for appid in sorted(self.lib_rows, key=lambda a: self.lib_data[a].lower()):
                r = self.lib_rows[appid]
                if q in r["text"]:
                    r["frame"].pack(fill="x", padx=4, pady=4)
            self.update_lib_info()

        def update_lib_info(self):
            q = self.lib_search.get().strip().lower()
            total = len(self.lib_data)
            if not self.steam_found:
                self.lib_info.configure(text=t("lib_nosteam"), text_color=RED)
            elif total == 0:
                self.lib_info.configure(text=t("lib_none"), text_color="gray60")
            elif q:
                shown = sum(1 for r in self.lib_rows.values() if q in r["text"])
                self.lib_info.configure(text=t("lib_showing", shown=shown, total=total),
                                        text_color="gray60")
            else:
                self.lib_info.configure(text=t("lib_total", n=total), text_color="gray60")

        def refresh_library_buttons(self):
            in_list = {g["id"] for g in self.games}
            theme = ctk.ThemeManager.theme["CTkButton"]
            for appid, r in self.lib_rows.items():
                if appid in in_list:
                    r["btn"].configure(text=t("added_btn"), state="disabled",
                                       fg_color=("gray75", "gray28"))
                else:
                    r["btn"].configure(text=t("add"), state="normal",
                                       fg_color=theme["fg_color"])

        def add_from_library(self, appid, name):
            if any(g["id"] == appid for g in self.games):
                return
            g = {"id": appid, "name": name}
            self.games.append(g)
            save_games(self.games)
            self.add_card(g)
            self.refresh_library_buttons()
            self.status.configure(text=t("added_fmt", name=name), text_color=GREEN)

        # ---- installs and hiding uninstalled games
        def rescan_async(self):
            def work():
                self.q.put(("installed", 0, scan_installed()))
            threading.Thread(target=work, daemon=True).start()

        def periodic_rescan(self):
            self.rescan_async()
            self.check_dll_integrity()
            self.after(30000, self.periodic_rescan)

        def on_tab_change(self):
            if self.tabs.get() == self.tab_library_name:
                self.rescan_async()

        def apply_installed(self, data):
            self.installed = data
            self.update_library(data)
            self.apply_visibility()

        def apply_visibility(self, force=False):
            hide = self.settings["hide_uninstalled"] and self.installed is not None
            new_hidden = {g["id"] for g in self.games
                          if hide and g["id"] not in self.installed}
            changed = force or new_hidden != self.hidden
            self.hidden = new_hidden
            if changed:
                for appid in new_hidden:
                    if appid in self.procs:
                        self.stop(appid)
                for g in self.games:
                    c = self.cards.get(g["id"])
                    if c:
                        c["frame"].pack_forget()
                for g in self.games:
                    c = self.cards.get(g["id"])
                    if c and g["id"] not in new_hidden:
                        c["frame"].pack(fill="x", padx=6, pady=6)
            if self.settings["hide_uninstalled"] and self.installed is None:
                note = t("no_steam_hiding")
            elif new_hidden:
                note = t("hidden_note", n=len(new_hidden))
            else:
                note = ""
            self.hidden_lbl.configure(text=note)

        # ---- event loops
        def pump(self):
            try:
                while True:
                    kind, appid, val = self.q.get_nowait()
                    if kind == "banner":
                        self.fetching.discard(appid)
                        p = banner_path(appid)
                        if not (p.exists() and p.stat().st_size > 0):
                            self.no_banner.add(appid)
                        self.apply_banner(appid)
                    elif kind == "dll":
                        self.dll_busy = False
                        ok = bool(appid)
                        if ok:
                            self.remember_dll_hash()
                        self.refresh_dll_ui(None if ok else val)
                        if ok:
                            self.status.configure(text=t("dll_ready_how", how=val),
                                                  text_color=GREEN)
                        elif find_dll():
                            self.status.configure(text=val, text_color=ORANGE)
                    elif kind == "installed":
                        self.apply_installed(val)
                    elif kind == "update":
                        if val:
                            self.show_update(val)
                    elif kind == "added":
                        if val is None:
                            self.status.configure(text=t("not_found"), text_color=RED)
                        else:
                            g = {"id": appid, "name": val}
                            self.games.append(g)
                            save_games(self.games)
                            self.add_card(g)
                            self.refresh_library_buttons()
                            self.apply_visibility(force=True)
                            if appid in self.hidden:
                                self.status.configure(text=t("added_hidden_fmt", name=val),
                                                      text_color=ORANGE)
                            else:
                                self.status.configure(text=t("added_fmt", name=val),
                                                      text_color=GREEN)
                    elif kind == "result":
                        if appid not in self.wanted:  # stopped in the meantime
                            continue
                        text, color = MESSAGES.get(val, (t("msg_failed"), RED))
                        log(f"{appid}: result {val or 'EMPTY'}")
                        if val == "OK":
                            self.retries.pop(appid, None)
                            self.failed.discard(appid)
                            self.started_at[appid] = time.time()
                            self.last_up.pop(appid, None)
                        else:
                            self.failed.add(appid)
                            n = self.retries.get(appid, 0)
                            if val == "INIT_FAIL" and (
                                    self.settings["watchdog"] or (self.auto_mode and n < 12)):
                                self.retries[appid] = n + 1
                                text, color = t("waiting_steam"), "gray60"
                                self.schedule_retry(appid, 15000)
                            else:
                                self.wanted.discard(appid)
                                self.set_btn(appid, False)
                        self.set_state(appid, text, color)
                    elif kind == "tray":
                        if val == "show":
                            self.show_window()
                        elif val == "start_all":
                            self.start_all(minimized=True)
                        elif val == "stop_all":
                            self.stop_all()
                        elif val == "quit":
                            self.quit_app()
                            return
            except queue.Empty:
                pass
            if not self.closing:
                self.after(100, self.pump)

        def poll_procs(self):
            if SHOW_FLAG.exists():  # a second launch asked us to come to the front
                try:
                    SHOW_FLAG.unlink()
                except OSError:
                    pass
                self.show_window()
            exited = []
            for appid, proc in list(self.procs.items()):
                rc = proc.poll()
                if rc is not None:
                    exited.append((appid, rc))

            # Several windows closing at about the same time can't be the user - it's Steam
            now = time.time()
            zero_closes = sum(1 for _, rc in exited if rc == 0)
            self.close_times = [x for x in self.close_times if now - x < 3] + [now] * zero_closes
            mass_close = len(self.close_times) >= 2

            for appid, rc in exited:
                log(f"{appid}: helper exited with code {rc}")
                self.stop(appid, keep_wanted=True)
                if rc == 0 and appid in self.wanted:
                    if mass_close and self.settings["watchdog"]:
                        log(f"{appid}: several windows closed together - assuming Steam shutdown")
                        self.failed.add(appid)
                        self.set_state(appid, t("waiting_steam"))
                        self.schedule_retry(appid, 15000)
                    else:
                        # closed by the user - or by Steam shutting down; find out in a moment
                        self.set_state(appid, t("closed"))
                        self.after(4000, lambda a=appid: self.check_closed(a))
                    continue
                if appid in self.wanted and appid not in self.failed and rc not in (2, 3, 4, 5):
                    if self.settings["watchdog"]:
                        steam_gone = rc == 10  # helper noticed that Steam was closed
                        self.failed.add(appid)
                        self.set_state(appid, t("waiting_steam") if steam_gone else t("restarting"))
                        self.schedule_retry(appid, 15000 if steam_gone else 5000)
                    else:
                        self.wanted.discard(appid)
                        self.set_btn(appid, False)
                        self.set_state(appid, t("idle"))

            # uptime on the cards + auto-stop timer
            limit = self.settings["timer_hours"] * 3600 if self.settings["timer_enabled"] else None
            for appid, began in list(self.started_at.items()):
                if appid not in self.procs:
                    continue
                elapsed = now - began
                if limit and elapsed >= limit:
                    log(f"{appid}: auto-stop timer reached")
                    self.stop(appid)
                    self.set_state(appid, t("timer_stopped"))
                    continue
                text = f"{t('in_game')} · {fmt_duration(elapsed)}"
                if self.last_up.get(appid) != text:
                    self.last_up[appid] = text
                    self.set_state(appid, text, GREEN)

            self.refresh_group_buttons()
            if self.tray:
                try:
                    self.tray.title = t("tray_running", n=len(self.procs))
                except Exception:
                    pass
            if not self.closing:
                self.after(1000, self.poll_procs)

        # ---- tray / window
        def start_tray(self):
            if pystray is None:
                return
            try:
                img = Image.open(ICON).convert("RGBA").resize((64, 64), Image.LANCZOS)

                def send(what):
                    return lambda icon, item: self.q.put(("tray", 0, what))
                menu = pystray.Menu(
                    pystray.MenuItem(t("tray_open"), send("show"), default=True),
                    pystray.MenuItem(t("tray_start_all"), send("start_all")),
                    pystray.MenuItem(t("tray_stop_all"), send("stop_all")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(t("tray_quit"), send("quit")))
                self.tray = pystray.Icon("Phantom", img, "Phantom", menu)
                self.tray.run_detached()
            except Exception:
                self.tray = None

        def show_window(self):
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()

        def hide_on_autostart(self):
            if self.tray:
                self.withdraw()
            else:
                self.iconify()

        def set_icon(self):
            try:  # customtkinter resets the icon shortly after startup, so set it later
                self.iconbitmap(str(ICON))
            except Exception:
                pass

        def on_close(self):
            if self.tray and self.settings["close_to_tray"]:
                self.withdraw()
                return
            self.quit_app()

        def quit_app(self):
            self.closing = True
            self.stop_all()
            self.wait_killers()
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.destroy()

    App().mainloop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--idle":
        parent = None
        if "--parent" in sys.argv:
            try:
                parent = int(sys.argv[sys.argv.index("--parent") + 1])
            except (ValueError, IndexError):
                pass
        run_idle(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "",
                 minimized="--minimized" in sys.argv[4:], parent=parent,
                 headless="--headless" in sys.argv[4:])
    else:
        if not acquire_single_instance(wait_seconds=12 if "--resume" in sys.argv else 0):
            request_show_existing()  # already running: just bring that window up
            sys.exit(0)
        run_gui()
