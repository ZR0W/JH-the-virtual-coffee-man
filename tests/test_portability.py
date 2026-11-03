"""Guards for the Windows + Ubuntu requirement and for keeping personal details out of deploy/."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coffee_kb"

POSIX_ONLY = [
    r"\bimport fcntl\b",
    r"\bimport pwd\b",
    r"\bimport grp\b",
    r"\bimport termios\b",
    r"\bos\.fork\b",
    r"\bos\.getuid\b",
    r"\bos\.symlink\b",
    r"\bsignal\.SIGALRM\b",
    r"['\"]/tmp/",
    r"['\"]/home/",
]


def test_no_posix_only_calls_in_src():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in POSIX_ONLY:
            assert not re.search(pat, text), f"{path.name}: {pat}"


def test_no_evasion_options_anywhere_in_src():
    for path in SRC.rglob("*.py"):
        if path.name == "safety.py":
            continue  # defines the forbidden list
        text = path.read_text(encoding="utf-8")
        for word in ("cookiefile", "cookiesfrombrowser", "impersonate", '"proxy"'):
            assert word not in text, f"{path.name}: {word}"
