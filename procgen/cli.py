"""Command-line interface for procgen."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extract import extract_repo
from .model import Asset
from .pslist import build_ps_wordlist
from .store import Policy, emit_asset, emit_repo
from .util import atomic_write, json_bytes, text_bytes

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
        "--no-ps",
        action="store_true",
        help="skip the derived legit_ps_linux process-command wordlist",
    )
    p.add_argument(
        "--ps-sample",
        type=int,
        default=25,
        metavar="N",
        help="tokens sampled per data list when building the ps wordlist (0 = all)",
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
    ps_repo_lines: list[str] = []

    for root in roots:
        if not root.is_dir():
            print(f"procgen: not a directory: {root}", file=sys.stderr)
            return 3
        result = extract_repo(root)
        if args.no_literals:
            result.assets = {k: v for k, v in result.assets.items() if v.kind != "literal"}

        if not args.no_ps:
            ps_lines = build_ps_wordlist(result.assets, sample=args.ps_sample)
            if ps_lines:
                result.assets["legit_ps_linux"] = Asset(
                    id="legit_ps_linux",
                    kind="ps",
                    name="legit_ps_linux",
                    source="(derived: signatures + data)",
                    items=ps_lines,
                )
                ps_repo_lines.extend(ps_lines)

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

    if not args.no_ps and ps_repo_lines:
        combined = sorted(set(ps_repo_lines))
        ps_asset = Asset(
            id="legit_ps_linux",
            kind="ps",
            name="legit_ps_linux",
            source="(derived: genact + fakeact)",
            items=combined,
        )
        ps_report = emit_asset(out_root, ps_asset, policy)
        total_stats["written"] += 1 if ps_report.action in ("created", "updated") else 0
        had_rejections = had_rejections or ps_report.action == "kept-prior"
        if not policy.dry_run:
            # A plain top-level copy so the file is where people expect it.
            atomic_write(out_root / "ps" / "legit_ps_linux.txt", text_bytes(combined))
        if not args.quiet:
            print(
                f"\n== ps wordlist ==\n"
                f"   [{ps_report.action}] legit_ps_linux.txt  {len(combined)} command lines"
            )

    # index.json is part of the committed corpus, so it may only contain facts
    # derived from the input, never run-relative counters.
    global_manifest = {
        "schema": 1,
        "tool": "procgen",
        "version": VERSION,
        "repos": index_entries,
        "summary": {
            "repos": len(index_entries),
            "assets": sum(e["assets"] for e in index_entries.values()),
            "items": sum(e["items"] for e in index_entries.values()),
            "wordlist_items": sum(e["wordlist_items"] for e in index_entries.values()),
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
