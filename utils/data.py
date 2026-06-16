"""
utils/data.py
Simple JSON persistence helpers used across all cogs.
Files are stored in the /data directory next to main.py.
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _path(filename: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, filename)


def load(filename: str) -> dict:
    """Load a JSON file and return its contents as a dict. Returns {} if missing or corrupt."""
    path = _path(filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save(filename: str, data: dict) -> None:
    """Save a dict to a JSON file, creating it if it doesn't exist."""
    path = _path(filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)