"""Validation, merge and emission.

The contract, in order of priority:

1. Never write a bad parse over good data.
2. Never lose a token that was already extracted (wordlists are additive).
3. Never rewrite a file whose bytes would not change (idempotent).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .model import Asset, RepoResult
from .util import (
    atomic_write,
    escape_for_line,
    json_bytes,
    sha256_bytes,
    text_bytes,
    unescape_from_line,
)

SCHEMA = 1


@dataclass
class Policy:
    min_retention: float = 0.5   # reject when new unique count < this * old
    shrink_floor: int = 5        # only enforce retention above this many items
    prune: bool = False          # allow wordlists to shrink to current parse
    dry_run: bool = False


@dataclass
class AssetReport:
    id: str
    kind: str
    name: str
    current: int
    previous: int
    merged: int
    action: str                  # created | updated | unchanged | kept-prior | skipped
    warnings: list[str]


# --- helpers ----------------------------------------------------------------


def _asset_dir(out: Path, asset: Asset) -> Path:
    return out / "modules" if asset.kind == "literal" else out


def _would_change(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() != data
    except OSError:
        return True


def read_previous(out: Path, asset: Asset) -> tuple[list[str], list[str]]:
    """Return (previous current-items, previous wordlist-items)."""
    base = _asset_dir(out, asset)
    prev_items: list[str] = []
    prev_wordlist: list[str] = []
    jpath = base / "json" / f"{asset.id}.json"
    if jpath.exists():
        try:
            obj = json.loads(jpath.read_text(encoding="utf-8"))
            prev_items = [str(x) for x in obj.get("items", [])]
        except (OSError, ValueError, TypeError):
            prev_items = []
    wpath = base / "wordlists" / f"{asset.id}.txt"
    if wpath.exists():
        try:
            prev_wordlist = [
                unescape_from_line(ln)
                for ln in wpath.read_text(encoding="utf-8").split("\n")
                if ln != ""
            ]
        except OSError:
            prev_wordlist = []
    return prev_items, prev_wordlist


def validate(asset: Asset, prev_items: list[str], policy: Policy) -> list[str]:
    """Return a list of reasons the parse should be rejected (empty = ok)."""
    reasons: list[str] = []
    items = asset.items
    for item in items:
        if "\x00" in item:
            reasons.append("nul-byte in item")
            break
    prev_unique = {i for i in prev_items if i != ""}
    new_unique = {i for i in items if i != ""}
    if prev_unique and not new_unique:
        reasons.append("empty parse over existing data")
    if (
        not policy.prune
        and len(prev_unique) >= policy.shrink_floor
        and len(new_unique) < len(prev_unique) * policy.min_retention
    ):
        reasons.append(
            f"suspicious shrink {len(new_unique)} < "
            f"{int(len(prev_unique) * policy.min_retention)} of {len(prev_unique)}"
        )
    return reasons


def wordlist_items(asset: Asset) -> list[str]:
    """Items suitable for the deduped wordlist view."""
    values = asset.wordlist_override if asset.wordlist_override is not None else asset.items
    return [v for v in values if v != ""]


# --- emission ---------------------------------------------------------------


def emit_asset(
    out: Path, asset: Asset, policy: Policy
) -> AssetReport:
    base = _asset_dir(out, asset)
    prev_items, prev_wordlist = read_previous(out, asset)

    reasons = validate(asset, prev_items, policy)
    if reasons:
        return AssetReport(
            id=asset.id,
            kind=asset.kind,
            name=asset.name,
            current=len(asset.items),
            previous=len(prev_items),
            merged=len(prev_wordlist),
            action="kept-prior",
            warnings=reasons,
        )

    current = list(asset.items)
    current_unique = {i for i in current if i != ""}

    if policy.prune:
        merged = sorted(current_unique)
    else:
        merged = sorted(set(prev_wordlist) | set(wordlist_items(asset)))

    action = "unchanged"
    created = not (
        (base / "json" / f"{asset.id}.json").exists()
        or (base / "raw" / f"{asset.id}.txt").exists()
    )

    json_obj = {
        "schema": SCHEMA,
        "id": asset.id,
        "kind": asset.kind,
        "name": asset.name,
        "source": asset.source,
        "count": len(current),
        "unique": len(current_unique),
        "wordlist_count": len(merged),
        "sha256": sha256_bytes(text_bytes(current)),
        "wordlist_sha256": sha256_bytes(text_bytes(merged)),
        "meta": asset.meta,
        "items": current,
    }

    targets = [
        (base / "raw" / f"{asset.id}.txt", text_bytes(current)),
        (base / "wordlists" / f"{asset.id}.txt", text_bytes(merged)),
        (base / "json" / f"{asset.id}.json", json_bytes(json_obj)),
    ]
    if policy.dry_run:
        changed = any([_would_change(path, data) for path, data in targets])
    else:
        # Materialise every write before testing: any() short-circuits, and a
        # short-circuit here would silently skip later files.
        results = [atomic_write(path, data) for path, data in targets]
        changed = any(results)

    if changed:
        action = "created" if created else "updated"
    return AssetReport(
        id=asset.id,
        kind=asset.kind,
        name=asset.name,
        current=len(current),
        previous=len(prev_items),
        merged=len(merged),
        action=action,
        warnings=[],
    )


def existing_ids(out: Path) -> set[str]:
    ids: set[str] = set()
    for jpath in (out / "json").glob("*.json"):
        ids.add(jpath.stem)
    for jpath in (out / "modules" / "json").glob("*.json"):
        ids.add("modules/" + jpath.stem)
    return ids


def _manifest_asset(asset: Asset, report: AssetReport | None) -> dict:
    entry = {
        "kind": asset.kind,
        "name": asset.name,
        "source": asset.source,
        "count": len(asset.items),
        "unique": len({i for i in asset.items if i != ""}),
        "meta": asset.meta,
    }
    if report is not None:
        entry["wordlist_count"] = report.merged
    return entry


def emit_repo(
    result: RepoResult, out: Path, policy: Policy
) -> tuple[dict, list[AssetReport], dict]:
    """Emit one repo. Returns (repo_manifest, reports, run_stats)."""
    reports: list[AssetReport] = []
    produced: set[str] = set()

    for asset in sorted(result.assets.values(), key=lambda a: a.id):
        report = emit_asset(out, asset, policy)
        reports.append(report)
        produced.add(("modules/" if asset.kind == "literal" else "") + asset.id)

    # Stale assets: present on disk, no longer produced. Kept, never deleted.
    stale = sorted(
        (existing_ids(out) - produced),
    )

    assets_meta = {}
    for asset in sorted(result.assets.values(), key=lambda a: a.id):
        if asset.kind == "literal":
            assets_meta["modules/" + asset.id] = _manifest_asset(asset, None)
        else:
            assets_meta[asset.id] = _manifest_asset(asset, None)

    # Pull merged counts from reports for the manifest.
    by_id = {r.id: r for r in reports}
    for key, entry in assets_meta.items():
        bare = key.split("/", 1)[1] if key.startswith("modules/") else key
        r = by_id.get(bare)
        if r is not None:
            entry["wordlist_count"] = r.merged

    manifest = {
        "schema": SCHEMA,
        "repo": {
            "name": result.name,
            "commit": result.commit,
            "flavor": result.flavor,
            "files_scanned": result.files_scanned,
        },
        "assets": assets_meta,
        "summary": {
            "assets": len(assets_meta),
            "items": sum(len(a.items) for a in result.assets.values()),
            "wordlist_items": sum(r.merged for r in reports),
        },
        "stale": stale,
        "warnings": sorted(result.warnings),
    }

    changed = sum(1 for r in reports if r.action in ("created", "updated"))
    stats = {
        "written": changed,
        "unchanged": sum(1 for r in reports if r.action == "unchanged"),
        "kept_prior": sum(1 for r in reports if r.action == "kept-prior"),
        "assets": len(reports),
    }
    return manifest, reports, stats
