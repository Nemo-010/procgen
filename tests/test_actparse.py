"""Tests for actparse: idempotency, structure-agnosticism, healing, safety."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from actparse.extract import extract_repo
from actparse.store import Policy, emit_repo


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_rust_repo(root: Path) -> None:
    write(root / "Cargo.toml", '[package]\nname = "fakegen"\nversion = "0.1.0"\n')
    write(root / "data" / "packages.txt", "alpha\nbeta\ngamma\n")
    write(
        root / "src" / "download.rs",
        """
use crate::data::PACKAGES;
static PACKAGES: &str = include_str!("../data/packages.txt");
pub struct Download;
impl Module for Download {
    fn name(&self) -> &'static str { "download" }
    async fn run(&self) {
        const EXTRA: &[&str] = &["foo", "bar", "baz"];
        print("Downloading something");
    }
}
""",
    )


def make_ts_repo(root: Path) -> None:
    write(root / "package.json", '{\n  "name": "fakets",\n  "version": "1.0.0"\n}\n')
    write(root / "data" / "packages.txt", "one\ntwo\nthree\n")
    write(
        root / "src" / "modules" / "pip.ts",
        """
import { loadData } from '../utils/dataLoader.js';
const WARNINGS = ['deprecated', 'insecure', 'outdated'];
async function pip() {
    const PACKAGES = await loadData('packages');
    writeLine('Installing something');
}
pip.signature = 'pip install -r requirements.txt';
export default pip;
""",
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="actparse-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_once(self, repo: Path, out: Path, **kw):
        policy = Policy(**kw)
        result = extract_repo(repo)
        manifest, reports, stats = emit_repo(result, out, policy)
        return result, manifest, reports, stats

    def snapshot(self, out: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(out)): p.read_bytes()
            for p in sorted(out.rglob("*"))
            if p.is_file()
        }


class TestExtraction(Base):
    def test_rust_data_and_arrays(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        result = extract_repo(repo)
        self.assertIn("data__packages", result.assets)
        self.assertEqual(
            result.assets["data__packages"].items, ["alpha", "beta", "gamma"]
        )
        self.assertIn("array__download__EXTRA", result.assets)
        self.assertEqual(
            result.assets["array__download__EXTRA"].items, ["foo", "bar", "baz"]
        )
        self.assertIn("module__download", result.assets)

    def test_ts_data_and_arrays(self) -> None:
        repo = self.tmp / "repo"
        make_ts_repo(repo)
        result = extract_repo(repo)
        self.assertEqual(result.flavor, "ts")
        self.assertIn("data__packages", result.assets)
        self.assertEqual(
            result.assets["data__packages"].items, ["one", "two", "three"]
        )
        self.assertIn("array__pip__WARNINGS", result.assets)

    def test_project_name_from_manifest(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        self.assertEqual(extract_repo(repo).name, "fakegen")


class TestIdempotency(Base):
    def test_second_run_is_noop(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        before = self.snapshot(out)
        _, _, _, stats = self.run_once(repo, out)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["unchanged"], stats["assets"])
        self.assertEqual(before, self.snapshot(out))

    def test_third_run_still_noop(self) -> None:
        repo = self.tmp / "repo"
        make_ts_repo(repo)
        out = self.tmp / "out"
        for _ in range(3):
            self.run_once(repo, out)
        before = self.snapshot(out)
        _, _, _, stats = self.run_once(repo, out)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(before, self.snapshot(out))


class TestStructureAgnostic(Base):
    def test_data_file_moved_still_found(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        # Move the data file elsewhere; the include path is now stale.
        (repo / "assets").mkdir()
        shutil.move(str(repo / "data" / "packages.txt"), repo / "assets" / "packages.txt")
        shutil.rmtree(repo / "data")
        result, _, reports, _ = self.run_once(repo, out)
        self.assertIn("data__packages", result.assets)
        self.assertEqual(
            result.assets["data__packages"].items, ["alpha", "beta", "gamma"]
        )
        # It is healed, not dropped.
        self.assertFalse(any(r.action == "kept-prior" for r in reports))

    def test_module_file_renamed(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        shutil.move(str(repo / "src" / "download.rs"), repo / "src" / "fetch.rs")
        result, manifest, _, _ = self.run_once(repo, out)
        # New identity appears, old data is retained as stale (never deleted).
        self.assertIn("module__fetch", result.assets)
        self.assertIn("modules/module__download", manifest["stale"])

    def test_variable_renamed(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        text = (repo / "src" / "download.rs").read_text()
        text = text.replace("EXTRA", "ADDITIONAL")
        (repo / "src" / "download.rs").write_text(text)
        result, manifest, _, _ = self.run_once(repo, out)
        self.assertIn("array__download__ADDITIONAL", result.assets)
        self.assertIn("array__download__EXTRA", manifest["stale"])


class TestSafety(Base):
    def test_empty_parse_keeps_prior(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        good = (out / "wordlists" / "data__packages.txt").read_bytes()
        # Upstream breaks: data file becomes empty.
        (repo / "data" / "packages.txt").write_text("")
        _, _, reports, _ = self.run_once(repo, out)
        rep = next(r for r in reports if r.id == "data__packages")
        self.assertEqual(rep.action, "kept-prior")
        self.assertEqual((out / "wordlists" / "data__packages.txt").read_bytes(), good)

    def test_shrink_guard(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "Cargo.toml", '[package]\nname = "big"\n')
        write(repo / "data" / "packages.txt", "\n".join(f"pkg{i}" for i in range(100)) + "\n")
        write(
            repo / "src" / "m.rs",
            'static P: &str = include_str!("../data/packages.txt");\n'
            'impl Module for M { fn name(&self) -> &\'static str { "m" } }\n',
        )
        out = self.tmp / "out"
        self.run_once(repo, out)
        good = (out / "wordlists" / "data__packages.txt").read_bytes()
        write(repo / "data" / "packages.txt", "pkg0\npkg1\n")
        _, _, reports, _ = self.run_once(repo, out)
        rep = next(r for r in reports if r.id == "data__packages")
        self.assertEqual(rep.action, "kept-prior")
        self.assertEqual((out / "wordlists" / "data__packages.txt").read_bytes(), good)

    def test_prune_allows_shrink(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "Cargo.toml", '[package]\nname = "big"\n')
        write(repo / "data" / "packages.txt", "\n".join(f"pkg{i}" for i in range(100)) + "\n")
        write(
            repo / "src" / "m.rs",
            'static P: &str = include_str!("../data/packages.txt");\n'
            'impl Module for M { fn name(&self) -> &\'static str { "m" } }\n',
        )
        out = self.tmp / "out"
        self.run_once(repo, out)
        write(repo / "data" / "packages.txt", "pkg0\npkg1\n")
        _, _, reports, _ = self.run_once(repo, out, prune=True)
        rep = next(r for r in reports if r.id == "data__packages")
        self.assertEqual(rep.action, "updated")
        self.assertEqual(rep.merged, 2)

    def test_wordlist_is_additive(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        self.run_once(repo, out)
        # Append a new token upstream; old tokens must survive.
        write(repo / "data" / "packages.txt", "alpha\nbeta\ngamma\ndelta\n")
        self.run_once(repo, out)
        items = (out / "wordlists" / "data__packages.txt").read_text().split("\n")
        for token in ("alpha", "beta", "gamma", "delta"):
            self.assertIn(token, items)

    def test_dry_run_writes_nothing(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        out = self.tmp / "out"
        result = extract_repo(repo)
        emit_repo(result, out, Policy(dry_run=True))
        self.assertFalse(out.exists())


class TestIdentity(Base):
    def test_declared_name_bug_uses_stem(self) -> None:
        # genact's simcity.rs really returns "mkinitcpio" from name(); identity
        # must follow the file, not the buggy declared name.
        repo = self.tmp / "repo"
        write(repo / "Cargo.toml", '[package]\nname = "buggy"\n')
        write(
            repo / "src" / "simcity.rs",
            "pub struct Simcity;\n"
            "impl Module for Simcity {\n"
            "    fn name(&self) -> &'static str { \"mkinitcpio\" }\n"
            "}\n",
        )
        write(
            repo / "src" / "mkinitcpio.rs",
            "pub struct Mkinitcpio;\n"
            "impl Module for Mkinitcpio {\n"
            "    fn name(&self) -> &'static str { \"mkinitcpio\" }\n"
            "}\n",
        )
        result = extract_repo(repo)
        self.assertIn("module__simcity", result.assets)
        self.assertIn("module__mkinitcpio", result.assets)
        self.assertEqual(
            result.assets["module__simcity"].meta["declared_name"], "mkinitcpio"
        )

    def test_unparseable_file_does_not_abort(self) -> None:
        repo = self.tmp / "repo"
        make_rust_repo(repo)
        write(repo / "src" / "broken.rs", "impl Module for Broken {\n")

        import actparse.extract as ex
        from unittest import mock

        real = ex.read_text

        def flaky(path):
            if Path(path).name == "broken.rs":
                raise RuntimeError("boom")
            return real(path)

        with mock.patch.object(ex, "read_text", side_effect=flaky):
            result = extract_repo(repo)

        self.assertIn("data__packages", result.assets)
        self.assertTrue(any("broken.rs" in w for w in result.warnings))


class TestHermetic(unittest.TestCase):
    def test_no_network_imports(self) -> None:
        pkg = Path(__file__).resolve().parent.parent / "actparse"
        forbidden = ("socket", "urllib", "http.client", "requests", "httpx", "aiohttp")
        for path in pkg.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(
                    f"import {name}", text, f"{path.name} imports {name}"
                )


if __name__ == "__main__":
    unittest.main()
