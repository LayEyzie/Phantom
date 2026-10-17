"""Every language must have exactly the English keys and the same {placeholders}."""
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import translations as T  # noqa: E402

en = T.STRINGS["en"]
fmt = string.Formatter()


def fields(text):
    return sorted(f for _, f, _, _ in fmt.parse(text) if f)


problems = []
for code, d in T.STRINGS.items():
    if set(d) != set(en):
        problems.append(f"{code}: key mismatch {sorted(set(en) ^ set(d))}")
    for k in en:
        if k in d and fields(en[k]) != fields(d[k]):
            problems.append(f"{code}: placeholders differ in {k}")
        if k in d and en[k].count("\n") != d[k].count("\n"):
            problems.append(f"{code}: line breaks differ in {k}")
assert set(T.LANGUAGES) == set(T.STRINGS), "LANGUAGES and STRINGS list different codes"

source = (ROOT / "phantom.py").read_text(encoding="utf-8")
used = set(re.findall(r'\bt\("([a-z_0-9]+)"', source))
unused = sorted(set(en) - used)
if unused:
    problems.append(f"unused translation keys: {unused}")

assert not problems, "\n".join(problems)
print(f"translations OK: {len(en)} keys x {len(T.STRINGS)} languages")
