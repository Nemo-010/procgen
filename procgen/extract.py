"""Extract generated content from a repository.

Two independent syntax adapters (Rust, TypeScript/JavaScript) feed one
RepoResult. Each adapter recovers:

  * data references   -- include_str!/loadData/loadCsv  -> wordlist files
  * flat literal arrays -- static/const NAME = [..]      -> inline wordlists
  * string literals    -- everything the module can print -> raw text corpus

None of it depends on file paths: an adapter is chosen by file suffix, a file
is treated as a module because of what it contains, and data files are found by
basename anywhere in the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import discovery, scan
from .discovery import RepoIndex
from .model import Asset, RepoResult
from .util import git_commit, read_text

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(text: str) -> str:
    return _SLUG.sub("_", text).strip("_") or "unnamed"


def _is_humanish(s: str) -> bool:
    """Whether a literal is plausible generated output worth a wordlist entry."""
    t = s.strip()
    if len(t) < 2:
        return False
    if not any(ch.isalpha() for ch in t):
        return False
    if t[0] == "\x1b" or t.startswith("\\u{1b}"):
        return False
    # pure format placeholders like {}, {:>12}, {name}
    if re.fullmatch(r"(?:\{[^}]*\})+", t):
        return False
    return True


class _Collector:
    """Accumulates assets while guaranteeing per-repo id uniqueness."""

    def __init__(self, result: RepoResult, index: RepoIndex):
        self.result = result
        self.index = index
        self.data_seen: dict[Path, str] = {}
        self.used_ids: set[str] = set()

    def unique_id(self, base: str, hint: str | None = None) -> str:
        if base not in self.used_ids:
            self.used_ids.add(base)
            return base
        # Disambiguate by a structural hint (parent dir) before numbering, so
        # ids stay readable and stable across runs.
        if hint:
            alt = f"{base}__{_safe(hint)}"
            if alt not in self.used_ids:
                self.used_ids.add(alt)
                return alt
        candidate = base
        n = 2
        while candidate in self.used_ids:
            candidate = f"{base}__{n}"
            n += 1
        self.used_ids.add(candidate)
        return candidate

    # -- data files ---------------------------------------------------------

    def add_data_file(
        self, path: Path, referenced: bool, literal: str | None = None,
        relocated: bool = False,
    ) -> None:
        path = path.resolve()
        if path in self.data_seen:
            return
        stem = path.stem
        asset_id = self.unique_id(f"data__{_safe(stem)}")
        self.data_seen[path] = asset_id
        text = read_text(path)
        lines = [ln.rstrip("\r") for ln in text.split("\n")]
        if lines and lines[-1] == "":
            lines.pop()
        items = [ln for ln in lines if ln != ""]
        meta = {
            "referenced": referenced,
            "extension": path.suffix,
            "bytes": len(text.encode("utf-8")),
        }
        if relocated:
            meta["relocated"] = True
        if literal and Path(literal).name != path.name:
            meta["relocated_from"] = literal
        asset = Asset(
            id=asset_id,
            kind="data",
            name=stem,
            source=self.index.rel(path),
            items=items,
            meta=meta,
        )
        if path.suffix == ".csv":
            asset.wordlist_override = _csv_fields(items)
        self.result.add(asset)

    # -- inline arrays ------------------------------------------------------

    def add_array(self, file_stem: str, var: str, kind: str, items: list[str], source: str) -> None:
        if not items:
            return
        asset_id = self.unique_id(f"array__{_safe(file_stem)}__{_safe(var)}")
        self.result.add(
            Asset(
                id=asset_id,
                kind="array",
                name=var,
                source=source,
                items=items,
                meta={"value_kind": kind, "file": file_stem},
            )
        )

    # -- text fragments -----------------------------------------------------

    def add_fragments(
        self, key: str, declared: str, source: str, literals: list[str],
        signature: str | None = None,
    ) -> None:
        if not literals and not signature:
            return
        asset_id = self.unique_id(f"module__{_safe(key)}")
        meta = {"declared_name": declared}
        if signature:
            meta["signature"] = signature
        self.result.add(
            Asset(
                id=asset_id,
                kind="literal",
                name=key,
                source=source,
                items=literals,
                meta=meta,
                wordlist_override=[s for s in literals if _is_humanish(s)],
            )
        )


def _csv_records(lines: list[str]) -> list[list[str]]:
    import csv
    import io

    try:
        return [row for row in csv.reader(io.StringIO("\n".join(lines))) if row]
    except Exception:
        return []


def _csv_fields(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in _csv_records(lines):
        for field in row:
            f = field.strip()
            if f and f not in seen:
                out.append(f)
                seen.add(f)
    return out


# --- Rust -------------------------------------------------------------------


def extract_rust(index: RepoIndex, result: RepoResult, col: _Collector) -> None:
    for path in index.sources(discovery.RUST_SUFFIXES):
        try:
            _process_rust_file(index, result, col, path)
        except Exception as exc:  # never let one file abort the run
            result.warnings.append(f"failed to parse {index.rel(path)}: {exc}")


def _process_rust_file(index: RepoIndex, result: RepoResult, col: _Collector, path) -> None:
    text = read_text(path)
    if not text:
        return
    stem = path.stem
    source = index.rel(path)

    for m in discovery.RUST_INCLUDE.finditer(text):
        resolved = index.resolve(path, m.group(1))
        if resolved is not None:
            direct = (path.parent / m.group(1)).resolve()
            col.add_data_file(
                resolved,
                referenced=True,
                literal=m.group(1),
                relocated=direct != resolved.resolve(),
            )
        else:
            result.warnings.append(f"missing data file {m.group(1)!r} (from {source})")

    for m in scan.RUST_DECL.finditer(text):
        parsed = scan.extract_flat_array(text, m.end() - 1, raw_strings=True)
        if parsed is None:
            continue
        value_kind, items = parsed
        col.add_array(stem, m.group(1), value_kind, items, source)

    if discovery.is_rust_module(text):
        module = discovery.rust_module_name(text, stem)
        literals = [v for v, _, _ in scan.iter_literals(text, raw_strings=True)]
        sig = discovery.rust_signature(text)
        if sig is not None:
            sig = scan.unescape(sig)
        col.add_fragments(stem, module, source, literals, sig)


# --- TypeScript / JavaScript -------------------------------------------------


def extract_ts(index: RepoIndex, result: RepoResult, col: _Collector) -> None:
    for path in index.sources(discovery.TS_SUFFIXES):
        try:
            _process_ts_file(index, result, col, path)
        except Exception as exc:  # never let one file abort the run
            result.warnings.append(f"failed to parse {index.rel(path)}: {exc}")


def _process_ts_file(index: RepoIndex, result: RepoResult, col: _Collector, path) -> None:
    text = read_text(path)
    if not text:
        return
    stem = path.stem
    source = index.rel(path)

    for m in discovery.TS_LOAD.finditer(text):
        func, name = m.group(1), m.group(2)
        suffix = ".csv" if "Csv" in func else ".txt"
        resolved = index.find_basename(name + suffix)
        if not resolved:
            # extension may differ upstream; fall back to any data file
            resolved = [p for p in index.data_files() if p.stem == name]
        if resolved:
            col.add_data_file(
                min(resolved, key=lambda p: (len(p.parts), str(p))),
                referenced=True,
                literal=name + suffix,
            )
        else:
            result.warnings.append(
                f"missing data file {name + suffix!r} (from {source})"
            )

    for m in scan.TS_DECL.finditer(text):
        parsed = scan.extract_flat_array(text, m.end() - 1, single=True, backtick=True)
        if parsed is None:
            continue
        value_kind, items = parsed
        col.add_array(stem, m.group(1), value_kind, items, source)

    if discovery.is_ts_module(text):
        module = discovery.ts_module_name(text, stem)
        literals = [
            v for v, _, _ in scan.iter_literals(text, single=True, backtick=True)
        ]
        col.add_fragments(stem, module, source, literals, discovery.ts_signature(text))

def _sweep_sibling_data(index: RepoIndex, result: RepoResult, col: _Collector) -> None:
    """Catch data files upstream added but has not referenced yet.

    Only directories that already hold a referenced data file are swept, so
    stray text files elsewhere never leak into the corpus.
    """
    referenced_dirs = {Path(p).parent for p in col.data_seen}
    for path in index.data_files():
        if path.resolve() in col.data_seen:
            continue
        if path.parent in referenced_dirs:
            col.add_data_file(path, referenced=False)


# --- entry point ------------------------------------------------------------


def _repo_name(index: RepoIndex) -> str:
    root = index.root
    for manifest, key in (("Cargo.toml", "name"), ("package.json", "name")):
        path = root / manifest
        if path.is_file():
            text = read_text(path)
            m = re.search(rf'^\s*{key}\s*[=:]\s*"([^"]+)"', text, re.M)
            if m:
                return m.group(1)
    return root.name


def _flavor(index: RepoIndex) -> str:
    has_rust = bool(index.sources(discovery.RUST_SUFFIXES))
    has_ts = bool(index.sources(discovery.TS_SUFFIXES))
    if has_rust and has_ts:
        return "mixed"
    return "rust" if has_rust else "ts" if has_ts else "unknown"


def extract_repo(root: Path) -> RepoResult:
    index = RepoIndex(root)
    result = RepoResult(
        root=str(index.root),
        name=_repo_name(index),
        flavor=_flavor(index),
        commit=git_commit(index.root),
        files_scanned=len(index.files),
    )
    col = _Collector(result, index)
    if index.sources(discovery.RUST_SUFFIXES):
        extract_rust(index, result, col)
    if index.sources(discovery.TS_SUFFIXES):
        extract_ts(index, result, col)
    _sweep_sibling_data(index, result, col)
    return result
