"""Derive a legitimate-process command-line wordlist from the extraction.

Target format is one full process command line per line, like
``pkgforge-security/Wordlists``' ``Misc/legit_ps_linux.txt``.

Content is grounded in what the generators actually emit, not in invented
templates:

* every module's ``signature()`` -- the exact command line it impersonates;
* the ``cc`` module's real compile and link invocations, reproduced from its
  own ``format!`` template and its own flag/file/package data;
* the ``mkinitcpio`` module's real ``-k .. -c .. -g ..`` invocation.

Modules that only print log output (cargo, terraform, julia, ...) contribute
their signature and nothing invented. Rendering is deterministic: the same
extraction always yields the same file.
"""

from __future__ import annotations

import re
from pathlib import PurePath

from .model import Asset

_WINDOWS = re.compile(r"\bbcdedit\b|\\\\|[A-Za-z]:\\", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_WRAPPERS = {"sudo", "doas", "env", "nohup", "nice", "time", "exec"}


def _program(signature: str) -> str:
    tokens = signature.strip().split()
    i = 0
    while i < len(tokens) and PurePath(tokens[i]).name in _WRAPPERS:
        i += 1
    if i >= len(tokens):
        return ""
    return PurePath(tokens[i]).name


def _clean(item: str) -> str:
    item = item.strip()
    return "" if not item or _CTRL.search(item) else item


def _pick(items, n: int) -> list[str]:
    """Deterministically sample up to *n* unique items, evenly spaced."""
    uniq = sorted({c for c in (_clean(x) for x in items) if c})
    if not uniq:
        return []
    if n <= 0 or len(uniq) <= n:
        return uniq
    if n == 1:
        return [uniq[0]]
    step = (len(uniq) - 1) / (n - 1)
    return [uniq[round(i * step)] for i in range(n)]


def _index(assets: dict[str, Asset]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for asset in assets.values():
        if asset.kind == "ps":
            continue
        merged.setdefault(asset.name, []).extend(asset.items)
    return merged


def _cc_lines(by_name: dict[str, list[str]], sample: int) -> list[str]:
    """Reproduce genact's cc output: `{compiler} -c {opt} {flags} -o {obj}`."""
    cfiles = [c for c in _pick(by_name.get("cfiles", ()), sample) if c.endswith(".c")]
    if not cfiles:
        return []

    declared = [c for c in (_clean(x) for x in by_name.get("COMPILERS", ())) if c]
    compiler = declared[0] if declared else "gcc"
    opts = _pick(by_name.get("FLAGS_OPT", ()), 0)
    opt = "-O2" if "-O2" in opts else (opts[0] if opts else "-O2")
    warn = _pick(by_name.get("FLAGS_WARN_BASE", ()), 1)[:1] + _pick(
        by_name.get("FLAGS_WARN", ()), 2
    )
    fflags = _pick(by_name.get("FLAGS_F", ()), 1)
    arch = _pick(by_name.get("FLAGS_ARCH", ()), 2)
    defs = _pick(by_name.get("FLAGS_DEF_BASE", ()), 1) + _pick(
        by_name.get("FLAGS_DEF", ()), 2
    )

    # Include dirs, as genact builds them: "-I" + dirname, up to 20 unique.
    include_dirs: list[str] = []
    for cfile in cfiles:
        parent = str(PurePath(cfile).parent)
        if parent not in (".", "") and parent not in include_dirs:
            include_dirs.append(parent)
    includes = ["-I" + d for d in include_dirs[:20]]

    flags = " ".join([opt, *warn, *fflags, *arch, *includes, *defs])
    lines = []
    objects = []
    for cfile in cfiles:
        obj = cfile[:-2] + ".o"
        objects.append(obj)
        lines.append(" ".join(f"{compiler} -c {flags} -o {obj}".split()))

    package = (_pick(by_name.get("packages", ()), 1) or ["app"])[0]
    libs = _pick(by_name.get("packages", ()), 3)
    link = " ".join([compiler, "-o", package, *objects, *("-l" + l for l in libs)])
    lines.append(" ".join(link.split()))
    return lines


def _mkinitcpio_lines(by_name: dict[str, list[str]]) -> list[str]:
    """Reproduce genact's mkinitcpio invocation for a couple of presets."""
    return [
        f"mkinitcpio -k /boot/vmlinuz-{p} -c /etc/mkinitcpio.conf "
        f"-g /boot/initramfs-{p}.img"
        for p in ("linux", "default")
    ]


def build_ps_wordlist(assets: dict[str, Asset], sample: int = 25) -> list[str]:
    """Return a deterministic, deduped list of real process command lines."""
    by_name = _index(assets)
    lines: list[str] = []

    for asset in sorted(assets.values(), key=lambda a: a.id):
        if asset.kind != "literal":
            continue
        signature = (asset.meta or {}).get("signature")
        if not signature:
            continue
        signature = " ".join(signature.split())
        if not signature or _WINDOWS.search(signature):
            continue

        program = _program(signature)
        if program in {"gcc", "cc", "clang"}:
            rendered = _cc_lines(by_name, sample)
            lines.extend(rendered or [signature])
        elif program == "mkinitcpio":
            lines.extend(_mkinitcpio_lines(by_name))
        else:
            lines.append(signature)

    cleaned = []
    for line in lines:
        line = " ".join(line.split())
        if line and not _WINDOWS.search(line):
            cleaned.append(line)
    return sorted(set(cleaned))
