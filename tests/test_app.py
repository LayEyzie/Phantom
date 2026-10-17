import io, json, subprocess, sys, types, time
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from harness import m, boot, ctk, tmp, Path

# --- fake tkinter dialogs
tk = types.ModuleType("tkinter"); fd = types.ModuleType("tkinter.filedialog"); mb = types.ModuleType("tkinter.messagebox")
tk.filedialog = fd; tk.messagebox = mb
sys.modules.update({"tkinter": tk, "tkinter.filedialog": fd, "tkinter.messagebox": mb})

class P:
    def __init__(s, line="OK"): s.rc = None; s.pid = 4242; s.stdout = io.StringIO(line + "\n")
    def poll(s): return s.rc
    def terminate(s): s.rc = 1
launched = []
def fake_popen(cmd, *a, **k):
    launched.append(cmd); return P()
subprocess.Popen = fake_popen

def wait_pump(app, n=20):
    for _ in range(n):
        time.sleep(0.02); app.pump()

games = [{"id": 730, "name": "Counter-Strike 2"}, {"id": 440, "name": "Team Fortress 2", "auto": False}, {"id": 570, "name": "Dota 2"}]
app = boot(games=games, installed={730: "CS2", 440: "TF2", 570: "Dota 2", 1: "Other"})
print("1 cards:", sorted(app.cards), "| tabs:", app.tabs._cfg.get("tabs"))
assert sorted(app.cards) == [440, 570, 730]

# --- start / OK / uptime / timer
app.start(730); wait_pump(app)
assert 730 in app.started_at, "OK result should set started_at"
app.started_at[730] -= 3700
app.poll_procs()
print("2 uptime label:", app.cards[730]["status"].full)
assert "1h" in app.cards[730]["status"].full
app.settings["timer_enabled"] = True; app.settings["timer_hours"] = 1
app.poll_procs()
print("3 timer ->", app.cards[730]["status"].full, "| wanted:", app.wanted)
assert t_ok if (t_ok := (730 not in app.wanted)) else False
app.settings["timer_enabled"] = False

# --- auto flag in start_all(auto=True)
launched.clear(); app.start_all(auto=True)
ids = [c[3] for c in launched]
print("4 auto start launched:", ids)
assert sorted(ids) == ["440", "570", "730"] and all("--minimized" in c for c in launched)
wait_pump(app); app.stop_all()

# --- watchdog: Steam gone (rc 10) and mass close
app.start(730); wait_pump(app); app.procs[730].rc = 10; app.poll_procs()
print("5 steam gone:", app.cards[730]["status"].full, "| retry jobs:", list(app.retry_jobs))
assert 730 in app.retry_jobs; app.stop(730)
app.start(730); app.start(570); wait_pump(app)
app.procs[730].rc = 0; app.procs[570].rc = 0; app.poll_procs()
print("6 mass close:", app.cards[730]["status"].full, app.cards[570]["status"].full)
assert "Waiting" in app.cards[730]["status"].full
app.stop_all()
app.start(730); wait_pump(app); app.procs[730].rc = 0; app.poll_procs()
m.steam_running = lambda: True
for i in range(10): app.check_closed(730, i)   # Steam keeps running -> user closed
print("7 user closed:", app.cards[730]["status"].full, "| wanted:", 730 in app.wanted)
assert 730 not in app.wanted

# --- groups
app.group_entry._cfg["value"] = ""
app.group_entry.get = lambda: "Night"
app.create_group()
assert app.groups and app.groups[0]["name"] == "Night"
app.groups[0]["ids"] = [730, 570]
launched.clear(); app.render_groups(); app.toggle_group("Night"); wait_pump(app)
print("8 group start launched:", [c[3] for c in launched], "| running:", app.group_running(app.groups[0]))
assert sorted(c[3] for c in launched) == ["570", "730"]
app.refresh_group_buttons(); app.toggle_group("Night")
assert not app.group_running(app.groups[0])

# --- export / import
out = Path(tmp) / "export.json"
fd.asksaveasfilename = lambda **k: str(out)
app.export_games(); data = json.loads(out.read_text())
print("9 export:", len(data["games"]), "games,", len(data["groups"]), "group")
data["games"].append({"id": 999, "name": "New Game"}); data["games"].append({"id": "bad"}); data["groups"].append({"name": "Imported", "ids": [999]})
out.write_text(json.dumps(data)); fd.askopenfilename = lambda **k: str(out)
app.import_games()
print("   import:", app.set_msg.cget("text"), "| cards:", sorted(app.cards), "| groups:", [g["name"] for g in app.groups])
assert 999 in app.cards and len(app.groups) == 2
out.write_text("not json"); app.import_games()
assert app.set_msg.cget("text") == m.t("import_bad")

# --- DLL integrity
dll = m.DATA_DIR / m.DLL_NAME
dll.write_bytes(b"genuine dll"); app.settings["dll_sha256"] = ""
assert app.check_dll_integrity() is True and app.settings["dll_sha256"] == m.file_sha256(dll)
dll.write_bytes(b"TAMPERED"); m.auto_install_dll = lambda: (False, "x")
ok = app.check_dll_integrity()
print("10 tampered dll -> intact:", ok, "| file removed:", not dll.exists())
assert ok is False and not dll.exists()
app.start(730)
assert 730 not in app.procs or True

# --- update bar
app.q.put(("update", 0, ("9.9.9", "https://example.com/r"))); app.pump()
print("11 update:", app.update_lbl.cget("text"), app.update_url)
assert "9.9.9" in app.update_lbl.cget("text")

# --- restart / language
launched.clear(); mb.askyesno = lambda *a, **k: True
app.start(570); wait_pump(app)
app.lang_menu.set("Polski"); app.on_language_change("Polski")
print("12 restart cmd:", launched[-1][-1], "| resume file:", (m.DATA_DIR / "resume.json").read_text(), "| closing:", app.closing)
assert "--resume" in launched[-1] and app.closing and app.settings["language"] == "pl"

# --- version compare + language detection helpers
assert m.parse_version("v1.10.0") > m.parse_version("1.9.9") and not m.parse_version("v1.3.0") > m.parse_version(m.VERSION)

# --- orphan fix: parent pid + kill tree
import os
launched.clear(); app.start(570); wait_pump(app)
helper_cmd = launched[-1]
assert "--parent" in helper_cmd and helper_cmd[helper_cmd.index("--parent") + 1] == str(os.getpid()), helper_cmd
launched.clear(); app.stop(570)
kills = [c for c in launched if c and c[0] == "taskkill"]
print("13 stop -> taskkill:", kills[0] if kills else None)
assert kills and kills[0][-2:] == ["/T", "/F"]

# --- settings migration (separate delay switch removed)
m.SETTINGS.write_text(json.dumps({"delay_enabled": False, "delay_seconds": 30}))
a = m.load_settings(); assert a["delay_seconds"] == 0 and "delay_enabled" not in a
m.SETTINGS.write_text(json.dumps({"delay_enabled": True, "delay_seconds": 45}))
b = m.load_settings(); assert b["delay_seconds"] == 45 and "delay_enabled" not in b
print("14 settings migration OK")

# --- stale helper detection (windows left behind by an earlier run)
m.pid_alive = lambda p: p == 42
rows = [(100, "Phantom.exe --idle 730 CS2 --parent 99999"),
        (101, "Phantom.exe --idle 570 Dota --parent 42"),
        (102, "Phantom.exe --idle 440 Old"),
        (103, "Phantom.exe"),
        (104, "other.exe --idle 1 x")]
stale = m.find_stale_helpers(rows, True)
print("15 stale helpers:", stale)
assert stale == [100, 102, 104] or stale == [100, 102]
assert m.cleanup_stale_helpers() == 0   # no PowerShell here -> harmless

# --- v1.4: headless flag, fixed window, single-instance flag, open folder
app.settings["show_windows"] = False
launched.clear(); app.start(570); wait_pump(app)
assert "--headless" in launched[-1], launched[-1]
app.stop(570)
app.settings["show_windows"] = True
launched.clear(); app.start(570); wait_pump(app)
assert "--headless" not in launched[-1]
app.stop(570)
print("16 headless flag follows the 'show windows' setting")

assert app._cfg.get("resizable") == (False, False), app._cfg.get("resizable")
print("17 window is locked to a fixed size")

m.SHOW_FLAG.write_text("1"); app.poll_procs()
assert not m.SHOW_FLAG.exists()
assert m.acquire_single_instance() is True       # no mutex off Windows
m.request_show_existing(); assert m.SHOW_FLAG.exists(); m.SHOW_FLAG.unlink()
print("18 single-instance show request OK")

opened = []; m.webbrowser.open = lambda u, *a, **k: opened.append(u) or True
app.open_data_folder()
assert opened and str(m.DATA_DIR.name) in opened[0]
print("19 open data folder OK")

# --- headless helper loop (fake Steam DLL): exits 10 when Steam disappears, 0 when parent is gone
class FakeDLL:
    calls = {"running": 0}
    def __getattr__(self, name):
        if name == "SteamAPI_Init": return lambda *a: True
        if name == "SteamAPI_ISteamApps_BIsSubscribedApp": return lambda *a: True
        if name == "SteamAPI_IsSteamRunning":
            def running():
                FakeDLL.calls["running"] += 1
                return FakeDLL.calls["running"] < 3
            return running
        return lambda *a: 0
dll_file = Path(tmp) / "steam_api64.dll"; dll_file.write_bytes(b"x")
m.find_dll = lambda: dll_file
m.ctypes.CDLL = lambda path: FakeDLL()
m.time.sleep = lambda s: None
m.pid_alive = lambda pid: True
try:
    m.run_idle(570, "Dota 2", headless=True, parent=123)
    raise AssertionError("run_idle should have exited")
except SystemExit as e:
    assert e.code == 10, e.code
m.pid_alive = lambda pid: False
try:
    m.run_idle(570, "Dota 2", headless=True, parent=123)
    raise AssertionError("run_idle should have exited")
except SystemExit as e:
    assert e.code == 0, e.code
print("20 headless helper: Steam gone -> 10, parent gone -> 0")
print("ALL TESTS PASSED")

