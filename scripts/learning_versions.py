"""Frozen runtime provenance for learning records.

This module observes source/config identity only. It has no authority over any
strategy, ranking, risk, focus, execution, or Paper decision.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from scripts.learning_contract import LEARNING_SCHEMA_VERSION

BASE_DIR = Path(__file__).resolve().parents[1]

COMPONENT_VERSION_LABELS = {
    "pipeline_version": "pipeline-v1",
    "diamond_version": "diamond-v3.1",
    "risk_version": "risk-v2",
    "focus_version": "focus-v3",
    "book_version": "book-v1",
    "paper_version": "paper-v1",
}

FINGERPRINT_FILES = (
    "machine_common.py",
    "scripts/flow_tracker.py",
    "scripts/diamond_filter_v3.py",
    "scripts/risk_engine.py",
    "scripts/focus_engine.py",
    "scripts/book_engine.py",
    "scripts/paper_engine.py",
)


class LearningVersionError(RuntimeError):
    pass


def _text(value):
    return str(value or "").strip()


def _git_sha(repo_root):
    env_sha = _text(os.getenv("POLYMARKET_GIT_SHA"))
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception as exc:
        raise LearningVersionError(
            "Git commit SHA unavailable; set POLYMARKET_GIT_SHA"
        ) from exc
    sha = _text(result.stdout)
    if not sha:
        raise LearningVersionError("Git commit SHA unavailable")
    return sha


def _config_fingerprint(repo_root):
    digest = hashlib.sha256()
    for relative in FINGERPRINT_FILES:
        path = Path(repo_root) / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise LearningVersionError(
                f"strategy fingerprint source unavailable: {relative}"
            ) from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return "CFG-" + digest.hexdigest()[:24]


@lru_cache(maxsize=8)
def _strategy_versions_cached(repo_root_text):
    repo_root = Path(repo_root_text)
    return {
        **COMPONENT_VERSION_LABELS,
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": _git_sha(repo_root),
        "config_fingerprint": _config_fingerprint(repo_root),
    }


def strategy_versions(repo_root=BASE_DIR):
    return dict(_strategy_versions_cached(str(Path(repo_root).resolve())))
