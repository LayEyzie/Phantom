"""Minimal fake of customtkinter so the App logic can be smoke-tested headlessly."""
AFTERS = []
INSTANCES = []


class D:
    def __init__(self, *a, **k):
        self._cfg = dict(k)
        self._children = []
        self.args = a
        if a and hasattr(a[0], "_children"):
            a[0]._children.append(self)

    def configure(self, **k):
        self._cfg.update(k)

    def cget(self, key):
        return self._cfg.get(key, "")

    def get(self):
        return self._cfg.get("value", "")

    def set(self, v):
        self._cfg["value"] = v

    def select(self):
        self._cfg["value"] = 1

    def deselect(self):
        self._cfg["value"] = 0

    def measure(self, text):
        return len(text) * 7

    def winfo_width(self):
        return 300

    def after(self, ms, fn=None, *a):
        AFTERS.append((ms, fn))
        return len(AFTERS)

    def after_cancel(self, i):
        pass

    def destroy(self):
        self._cfg["destroyed"] = True

    def tab_get(self):
        return self._cfg.get("current", "")

    def add(self, name):
        d = D()
        d._cfg["tab"] = name
        self._cfg.setdefault("tabs", []).append(name)
        return d

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class CTk(D):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        INSTANCES.append(self)

    def mainloop(self):
        pass

    def state(self, *a):
        return "normal"

    def resizable(self, w=None, h=None):
        self._cfg["resizable"] = (w, h)


class CTkTabview(D):
    def get(self):
        return self._cfg.get("current", "")


class StringVar:
    def __init__(self, value=""):
        self.v = value
        self.cbs = []

    def get(self):
        return self.v

    def set(self, v):
        self.v = v
        for cb in self.cbs:
            cb()

    def trace_add(self, mode, cb):
        self.cbs.append(cb)


for _n in ("CTkFrame", "CTkLabel", "CTkButton", "CTkEntry", "CTkSwitch", "CTkCheckBox",
           "CTkScrollableFrame", "CTkFont", "CTkImage", "CTkOptionMenu", "CTkToplevel",
           "CTkTextbox", "CTkProgressBar"):
    globals()[_n] = type(_n, (D,), {})


class ThemeManager:
    theme = {"CTkButton": {"fg_color": ("#3b8ed0", "#1f6aa5"), "hover_color": ("#36719f", "#144870")}}


def set_appearance_mode(*a): pass
def set_default_color_theme(*a): pass
