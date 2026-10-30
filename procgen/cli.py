"""Command-line interface for procgen."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extract import extract_repo
from .store import Policy, emit_repo
from .util import atomic_write, json_bytes

VERSION = "1.0.0"


def _autodetect(start: Path) -> list[Path]:
    """Find candidate repos: the directory itself and its children."""
    found: list[Path] = []
    seen: set[Path] = set()

    def consider(path: Path) -> None:
        path = path.resolve()
        if path in seen or not path.is_dir():
            return
        if (path / "Cargo.toml").is_file() or (path / "package.json").is_file():
            seen.add(path)
            found.append(path)

    consider(start)
    try:
        for child in sorted(start.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                consider(child)
    except OSError:
        pass
    return found


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="procgen",
        description="Extract what fakeact/genact generate into wordlists, raw text and JSON.",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="PATH",
        help="repository to parse (repeatable). Default: auto-detect under cwd.",
    )
    p.add_argument(
        "-o",
        "--out",
        default="extracted",
        metavar="DIR",
        help="output directory (default: ./extracted)",
    )
    p.add_argument(
        "--min-retention",
        type=float,
        default=0.5,
        help="reject a parse that keeps less than this fraction of prior unique items",
    )
    p.add_argument(
        "--shrink-floor",
        type=int,
        default=5,
        help="only apply the shrink guard above this many prior items",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="let wordlists shrink to the current parse instead of union-merging",
    )
    p.add_argument(
        "--no-literals",
        action="store_true",
        help="skip the per-module string-literal corpus",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="compute and report, write nothing"
    )
    p.add_argument("--quiet", action="store_true", help="only print the summary")
    p.add_argument("--version", action="version", version=f"procgen {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    roots = [Path(r) for r in args.repo]
    if not roots:
        roots = _autodetect(Path.cwd())
    if not roots:
        print("procgen: no repositories given and none auto-detected", file=sys.stderr)
        return 3

    out_root = Path(args.out)
    policy = Policy(
        min_retention=args.min_retention,
        shrink_floor=args.shrink_floor,
        prune=args.prune,
        dry_run=args.dry_run,
    )

    index_entries: dict[str, dict] = {}
    total_stats = {"written": 0, "unchanged": 0, "kept_prior": 0, "assets": 0}
    had_rejections = False

    for root in roots:
        if not root.is_dir():
            print(f"procgen: not a directory: {root}", file=sys.stderr)
            return 3
        result = extract_repo(root)
        if args.no_literals:
            result.assets = {k: v for k, v in result.assets.items() if v.kind != "literal"}

        repo_out = out_root / result.name
        manifest, reports, stats = emit_repo(result, repo_out, policy)

        if policy.dry_run:
            # Recompute what would change without touching disk is handled by
            # emit_asset's byte comparison; on dry-run we skip the write layer.
            pass
        else:
            atomic_write(repo_out / "manifest.json", json_bytes(manifest))

        for key in ("written", "unchanged", "kept_prior", "assets"):
            total_stats[key] += stats[key]
        had_rejections = had_rejections or stats["kept_prior"] > 0

        index_entries[result.name] = {
            "commit": result.commit,
            "flavor": result.flavor,
            "manifest": f"{result.name}/manifest.json",
            "assets": manifest["summary"]["assets"],
            "items": manifest["summary"]["items"],
            "wordlist_items": manifest["summary"]["wordlist_items"],
            "warnings": len(manifest["warnings"]),
        }

        if not args.quiet:
            print(f"\n== {result.name} ({result.flavor}) ==")
            print(f"   root:   {result.root}")
            print(f"   commit: {result.commit or '-'}")
            print(f"   files:  {result.files_scanned}")
            for rep in reports:
                flag = {
                    "created": "+",
                    "updated": "~",
                    "unchanged": ".",
                    "kept-prior": "!",
                    "skipped": "?",
                }.get(rep.action, " ")
                line = (
                    f"   [{flag}] {rep.id:<44} "
                    f"{rep.current:>7} items  {rep.merged:>7} wordlist"
                )
                if rep.warnings:
                    line += "  <- " + "; ".join(rep.warnings)
                print(line)
            for warn in manifest["warnings"]:
                print(f"   warning: {warn}")
            if manifest["stale"]:
                print(f"   stale (kept): {', '.join(manifest['stale'])}")

    global_manifest = {
        "schema": 1,
        "tool": "procgen",
        "version": VERSION,
        "repos": index_entries,
        "summary": {
            "repos": len(index_entries),
            "assets": total_stats["assets"],
            "written": total_stats["written"],
            "unchanged": total_stats["unchanged"],
            "kept_prior": total_stats["kept_prior"],
        },
    }
    if not policy.dry_run:
        atomic_write(out_root / "index.json", json_bytes(global_manifest))

    print(
        f"\nsummary: {total_stats['assets']} assets, "
        f"{total_stats['written']} written, "
        f"{total_stats['unchanged']} unchanged, "
        f"{total_stats['kept_prior']} kept-prior"
        + (" (dry-run)" if policy.dry_run else "")
    )
    return 4 if had_rejections and not args.dry_run else 0
