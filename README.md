# procgen

Extract everything [genact](https://github.com/svenstaro/genact) and
[fakeact](https://github.com/CH563/fakeact) can generate — the wordlists, the
inline string arrays and the raw text fragments — into plain wordlists, raw
text and JSON. It is built to keep working when upstream reshuffles its tree,
renames a variable or fixes (or introduces) a bug in a module.

```
python3 -m procgen --repo ../genact --repo ../fakeact --out extracted
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
  ps/
    legit_ps_linux.txt       # derived legit-process command-line wordlist
```

Four asset kinds are recovered:

| kind      | source                                   | example                         |
|-----------|------------------------------------------|---------------------------------|
| `data`    | `include_str!`, `loadData`/`loadCsv`      | `data/packages.txt`             |
| `array`   | `static NAME: &[&str] = &[..]`, `const N = [..]` | `FLAGS_WARN`, `HTTP_CODES` |
| `literal` | every string literal in a module body     | `"Sending build context"`        |
| `ps`      | module signatures + grounded expansions   | `cargo install --locked memchr`  |

## The `ps` wordlist

`ps/legit_ps_linux.txt` is a Linux process allowlist in the style of
[pkgforge-security/Wordlists](https://github.com/pkgforge-security/Wordlists)'s
`Misc/legit_ps_linux.txt`: one full command line per line. It is built from two
faithful grounds — each module's `signature()` (the exact command line it
impersonates) and deterministic expansions that combine that signature with the
module's own extracted vocabulary (packages, C files, compiler flags, docker
tags, terraform resources, Julia packages, …). Modules without a matching
profile still contribute their signature verbatim, so new upstream modules never
vanish from the list. Windows-only invocations are dropped.

Expansion is deterministic: large token lists are sampled with a fixed evenly
spaced stride (`--ps-sample N`, default 25; `0` keeps everything), so the same
extraction always yields the same file. `--no-ps` skips it.

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
--no-ps              skip the derived legit_ps_linux wordlist
--ps-sample N        tokens sampled per data list for the ps wordlist (0 = all)
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

`.github/workflows/update.yml` polls the HEAD revision of both upstreams
hourly. When either has moved it re-extracts, re-runs the test suite, and
commits only if the corpus actually changed and every parse passed validation.
Upstream repositories cannot push events to us, so polling is the honest
mechanism; `workflow_dispatch` (or a `repository_dispatch` of type
`upstream-changed`) triggers an immediate run when you do not want to wait.
