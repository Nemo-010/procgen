"""Data model shared by the extraction and storage layers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Asset:
    """One extractable unit of generated content."""

    id: str            # stable, filesystem-safe, repo-unique
    kind: str          # "data" | "array" | "literal"
    name: str          # human name (basename / variable / module)
    source: str        # repo-relative path it came from
    items: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    # Derived view for the wordlist when it differs from items (e.g. CSV
    # columns, filtered literals). Never serialised into the JSON output.
    wordlist_override: list[str] | None = None


@dataclass
class RepoResult:
    root: str
    name: str
    flavor: str        # "rust" | "ts" | "mixed"
    commit: str | None
    files_scanned: int
    assets: dict[str, Asset] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, asset: Asset) -> None:
        existing = self.assets.get(asset.id)
        if existing is None:
            self.assets[asset.id] = asset
            return
        # Same id reached twice: keep a superset so nothing is lost.
        seen = set(existing.items)
        for item in asset.items:
            if item not in seen:
                existing.items.append(item)
                seen.add(item)
