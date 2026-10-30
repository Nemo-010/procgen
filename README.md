# actparse

Extract everything [genact](https://github.com/svenstaro/genact) and
[fakeact](https://github.com/CH563/fakeact) can generate — the wordlists, the
inline string arrays and the raw text fragments — into plain wordlists, raw
text and JSON. It is built to keep working when upstream reshuffles its tree,
renames a variable or fixes (or introduces) a bug in a module.

```
python3 -m actparse --repo ../genact --repo ../fakeact --out extracted
```

## What it produces

```
extracted/
  index.json                 # cross-repo summary
  <repo>/
    manifest.json            # per-repo asset index + checksums
    wordlists/<id>.txt        # deduped, sorted, cumulative tokens
    raw/<id>.txt              # faithful current extraction, source order
    json/<id>.json            # structured: metadata + items + hashes
    modules/                  # same three views, per generator module
```

Three asset kinds are recovered:

| kind      | source                                   | example                         |
|-----------|------------------------------------------|---------------------------------|
| `data`    | `include_str!`, `loadData`/`loadCsv`      | `data/packages.txt`             |
| `array`   | `static NAME: &[&str] = &[..]`, `const N = [..]` | `FLAGS_WARN`, `HTTP_CODES` |
| `literal` | every string literal in a module body     | `"Sending build context"`        |

## The guarantees

**Structure agnostic.** Nothing keys off a path. Files are chosen by suffix,
a module is a module because it contains `impl Module for` / `.signature =`,
and data files are found by *basename anywhere in the tree*. Move `data/` to
`assets/`, rename `download.rs` to `fetch.rs`, or rename `EXTRA` to
`ADDITIONAL` and the parser still finds the content.

**Self-healing.** A moved data file is re-located by basename. A file that
fails to parse is skipped with a warning instead of aborting the run. When a
module or variable is renamed, the old asset is kept on disk as `stale` — its
data is never deleted.

**Hermetic.** Python standard library only, no network, no clock in the
output. The revision recorded comes from `.git`, so a given checkout always
produces the same bytes.

**Idempotent.** Output is canonical (sorted keys, sorted wordlists, no
timestamps). A second run over unchanged input writes zero files.

**Never destroys good data.** Before writing, each parse is validated:

* an empty parse over non-empty prior data is rejected;
* a parse that keeps less than `--min-retention` (default 50%) of the prior
  unique items is rejected as a suspicious shrink;
* a rejected parse leaves the previous files untouched (`kept-prior`);
* accepted wordlists are **union-merged** with what was there before, so a
  token that ever appeared is never lost.

`--prune` opts into authoritative replacement (wordlists may then shrink);
`--dry-run` computes and reports without writing.

## Options

```
--repo PATH          repository to parse (repeatable; auto-detected if omitted)
-o, --out DIR        output directory (default ./extracted)
--min-retention F    shrink guard threshold (default 0.5)
--shrink-floor N     only guard above this many prior items (default 5)
--prune              let wordlists shrink to the current parse
--no-literals        skip the per-module string-literal corpus
--dry-run            report only, write nothing
--quiet              print just the summary
```

Exit code is `4` when any asset was kept-prior (a parse was rejected), so CI
can surface a regression without failing the build.

## Running the tests

```
python3 -m unittest discover -s tests
```

The suite covers extraction for both syntaxes, byte-level idempotency,
structure-agnostic healing (moved files, renamed modules and variables), the
shrink/empty guards, additive wordlists, and the absence of network imports.

## Keeping it current

`.github/workflows/update.yml` runs the extractor on a schedule against fresh
checkouts of both upstreams and commits only when the output actually changed
and every parse passed validation.
