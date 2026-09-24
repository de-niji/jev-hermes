"""Shared test helpers: import the scripts and build fake Jev answers without network."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EXAMPLES = ROOT / "examples"
sys.path.insert(0, str(SCRIPTS))

# Never touch the real API: a file:// URL makes urllib fail locally with URLError.
OFFLINE_URL = "file:///nonexistent/jev-decisions"


def choice(value: str, confidence: float) -> dict:
    return {"type": "choice", "choice": value, "confidence": confidence}


def noul(p: float) -> dict:
    return {"type": "noul", "noul": p}
