"""Repository discovery.

Nothing here assumes a directory layout. Files are classified by *content*
suffix, module files are found by *what they contain*, and data files are
resolved by *basename search* so a moved or renamed data directory still works.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Build/artifact directories that never contain generator sources or data.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "target",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    ".cache",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".gradle",
    ".turbo",
}

RUST_SUFFIXES = {".rs"}
TS_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".cjs"}
DATA_SUFFIXES = {".txt", ".csv", ".tsv", ".list", ".dic", ".wordlist", ".words"}


def _is_subproject(path: Path, root: Path) -> bool:
    """A nested directory with its own manifest is a separate project."""
    if path == root:
        return False
    return (path / "Cargo.toml").is_file() or (path / "package.json").is_file()


def walk(root: Path):
    """Yield every file under *root*, skipping artifacts and sub-projects."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if name in SKIP_DIRS or name.startswith("."):
                continue
            sub = d / name
            if _is_subproject(sub, root):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            yield d / name


class RepoIndex:
    """A single walk that answers both 'which sources' and 'where is X'."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.files: list[Path] = []
        self.by_basename: dict[str, list[Path]] = {}
        for path in walk(self.root):
            self.files.append(path)
            self.by_basename.setdefault(path.name, []).append(path)

    def sources(self, suffixes) -> list[Path]:
        return [p for p in self.files if p.suffix in suffixes]

    def data_files(self) -> list[Path]:
        return [p for p in self.files if p.suffix in DATA_SUFFIXES]

    def find_basename(self, name: str) -> list[Path]:
        return list(self.by_basename.get(name, ()))

    def resolve(self, source_file: Path, rel: str) -> Path | None:
        """Resolve an include path: literal location first, then by basename."""
        literal = (Path(source_file).parent / rel).resolve()
        if literal.is_file():
            return literal
        base = Path(rel).name
        hits = self.find_basename(base)
        if hits:
            return min(hits, key=lambda p: (len(p.parts), str(p)))
        return None

    def rel(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root))
        except ValueError:
            return str(path)


# --- module / generator detection -------------------------------------------

RUST_MODULE = re.compile(r"impl\s+(?:<[^>]*>\s*)?Module\s+for\s+([A-Za-z_]\w*)")
RUST_NAME = re.compile(
    r"fn\s+name\s*\(\s*&self\s*\)\s*->\s*&\s*'static\s+str\s*\{\s*\"([^\"]+)\""
)
RUST_INCLUDE = re.compile(r"include_(?:str|bytes)!\s*\(\s*\"([^\"]+)\"\s*\)")

TS_SIGNATURE = re.compile(r"([A-Za-z_$][\w$]*)\s*\.\s*signature\s*=")
TS_DEFAULT = re.compile(r"export\s+default\s+([A-Za-z_$][\w$]*)")
TS_LOAD = re.compile(
    r"(loadData|loadCsv|loadDataFile|loadCsvFile)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
TS_FETCH_DATA = re.compile(r"/data/\$\{[^}]+\}|['\"]data/([^'\"]+)['\"]")


def is_rust_module(text: str) -> bool:
    return bool(RUST_MODULE.search(text))


def is_ts_module(text: str) -> bool:
    return bool(TS_SIGNATURE.search(text) or (TS_DEFAULT.search(text) and "function" in text))


def rust_module_name(text: str, fallback: str) -> str:
    m = RUST_NAME.search(text)
    return m.group(1) if m else fallback


def ts_module_name(text: str, fallback: str) -> str:
    m = TS_SIGNATURE.search(text)
    if m:
        return m.group(1)
    m = TS_DEFAULT.search(text)
    return m.group(1) if m else fallback
