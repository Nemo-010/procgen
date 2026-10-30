"""Small deterministic helpers: hashing, atomic IO, line encoding."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def escape_for_line(item: str) -> str:
    """Make a token safe for a newline-delimited file, reversibly."""
    return (
        item.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    )


def unescape_from_line(line: str) -> str:
    """Inverse of :func:`escape_for_line`."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def text_bytes(items) -> bytes:
    """Newline-delimited file. Empty list -> empty file."""
    if not items:
        return b""
    return ("\n".join(escape_for_line(i) for i in items) + "\n").encode("utf-8")


def json_bytes(obj) -> bytes:
    """Canonical JSON: sorted keys, fixed indent, trailing newline."""
    return (
        json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, data: bytes) -> bool:
    """Write *only if the content differs*. Returns True when the file changed.

    This is what makes repeated runs a no-op: identical bytes never touch the
    filesystem, so mtimes stay stable and the run is idempotent.
    """
    path = Path(path)
    if path.exists():
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def git_commit(root: Path) -> str | None:
    """Deterministic per-checkout revision, read without a network call."""
    root = Path(root)
    head = root / ".git" / "HEAD"
    if not head.exists():
        return None
    try:
        ref = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if ref.startswith("ref:"):
        ref_path = root / ".git" / ref[4:].strip()
        if ref_path.exists():
            try:
                return ref_path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        packed = root / ".git" / "packed-refs"
        if packed.exists():
            name = ref[4:].strip()
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + name):
                        return line.split()[0]
            except OSError:
                return None
        return None
    return ref or None
