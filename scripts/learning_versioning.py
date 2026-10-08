"""Runtime version attribution for learning records.

The values in this module are observational metadata only. They never alter
Diamond, Risk, Focus, Book, Paper, or Telegram decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from scripts.learning_contract import LEARNING_SCHEMA_VERSION

BASE_DIR = Path(__file__).resolve().parents[1]

PIPELINE_VERSION = "pipeline-v3"
DIAMOND_VERSION = "diamond-v3.1"
RISK_VERSION = "risk-v2"
FOCUS_VERSION = "focus-v3"
BOOK_VERSION = "book-v1"
PAPER_VERSION = "paper-v1"

_SOURCE_PATHS = (
    "machine_common.py",
    "scripts/flow_tracker.py",
    "scripts/diamond_filter_v3.py",
    "scripts/risk_engine.py",
    "scripts/focus_engine.py",
    "scripts/book_engine.py",
    "scripts/paper_engine.py",
)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class LearningVersionError(RuntimeError):
    pass


def _git_sha() -> str:
    supplied = str(os.getenv("POLYMARKET_RUNTIME_GIT_SHA") or "").strip()
    if supplied:
        if not _SHA_RE.fullmatch(supplied):
            raise LearningVersionError("POLYMARKET_RUNTIME_GIT_SHA must be a 40-hex commit SHA")
        return supplied.lower()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        raise LearningVersionError("git commit SHA unavailable") from exc

    value = result.stdout.strip()
    if not _SHA_RE.fullmatch(value):
        raise LearningVersionError("git rev-parse returned an invalid commit SHA")
    return value.lower()


def _source_fingerprint() -> str:
    payload = {}
    for relative in _SOURCE_PATHS:
        path = BASE_DIR / relative
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise LearningVersionError(f"source fingerprint unavailable: {relative}") from exc
        payload[relative] = hashlib.sha256(raw).hexdigest()

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "SRC-" + hashlib.sha256(canonical).hexdigest()


# Captured once at process import. A long-running worker therefore cannot be
# silently relabelled by a later git pull while its already-loaded code remains
# in memory.
RUNTIME_GIT_SHA = _git_sha()
RUNTIME_CONFIG_FINGERPRINT = _source_fingerprint()


def runtime_strategy_versions() -> dict:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "diamond_version": DIAMOND_VERSION,
        "risk_version": RISK_VERSION,
        "focus_version": FOCUS_VERSION,
        "book_version": BOOK_VERSION,
        "paper_version": PAPER_VERSION,
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": RUNTIME_GIT_SHA,
        "config_fingerprint": RUNTIME_CONFIG_FINGERPRINT,
    }
