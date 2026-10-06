"""Shared, side-effect-free storage and freshness helpers."""
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_DATA_AGE_SECONDS = 120


def age_seconds(value, now=None):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - dt).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None


def fresh(value, now=None):
    age = age_seconds(value, now)
    return age is not None and -5 <= age <= MAX_DATA_AGE_SECONDS


def finite_number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def save_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def tail_jsonl(path, limit=10000):
    """Read only the file tail; ignore incomplete/malformed JSON records."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            pos = stream.tell()
            chunks = []
            lines = 0
            while pos and lines <= limit:
                size = min(pos, 65536)
                pos -= size
                stream.seek(pos)
                chunk = stream.read(size)
                chunks.append(chunk)
                lines += chunk.count(b'\n')
            raw = b''.join(reversed(chunks)).splitlines()
            if pos:
                raw = raw[1:]
        result = []
        for line in raw[-limit:]:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    result.append(row)
            except (ValueError, UnicodeDecodeError):
                pass
        return result
    except FileNotFoundError:
        return []
