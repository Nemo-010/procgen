"""Derive a legitimate-process command-line wordlist from the extraction.

Target format is one full process command line per line, like
``pkgforge-security/Wordlists``' ``Misc/legit_ps_linux.txt``.

Two grounds are used, both faithful to the source:

* each module's ``signature()`` -- the exact command line it impersonates;
* grounded expansions that combine a signature with the module's own
  extracted vocabulary (packages, C files, compiler flags, docker tags, ...).

Expansion is deterministic: large token lists are sampled with a fixed,
evenly spaced stride, so the same extraction always yields the same file.
Modules without a matching profile still contribute their signature verbatim,
so new or renamed upstream modules never disappear from the corpus.
"""

from __future__ import annotations

import re
from pathlib import PurePath

from .model import Asset

# Windows-only invocations do not belong in a Linux process allowlist.
_WINDOWS = re.compile(r"\bbcdedit\b|\\\\|[A-Za-z]:\\", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_JULIA_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_WRAPPERS = {"sudo", "doas", "env", "nohup", "nice", "time", "exec"}


def _program(signature: str) -> str:
    """Program basename, skipping common command wrappers like sudo/env."""
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


class _Ctx:
    """Lookups and deterministic sampling handed to each profile."""

    def __init__(self, sig: str, by_name: dict[str, list[str]], sample: int):
        self.sig = sig
        self._by_name = by_name
        self._sample = sample

    def all(self, name: str) -> list[str]:
        return list(self._by_name.get(name, ()))

    def sample(self, name: str) -> list[str]:
        return _pick(self._by_name.get(name, ()), self._sample)

    def first_col(self, name: str) -> list[str]:
        values = []
        for line in self._by_name.get(name, ()):
            first = _clean(line.split(",", 1)[0])
            if first and _JULIA_ID.match(first):
                values.append(first)
        return _pick(values, self._sample)


# --- profiles, keyed by the signature's program basename --------------------


def _cargo(c: _Ctx) -> list[str]:
    out = [
        c.sig,
        "cargo build --release",
        "cargo build --target x86_64-unknown-linux-musl --release",
        "cargo test --all-features",
        "cargo clippy --all-targets -- -D warnings",
    ]
    for pkg in c.sample("packages"):
        out.append(f"cargo install --locked {pkg}")
    return out


def _cc(c: _Ctx) -> list[str]:
    compilers = c.all("COMPILERS") or ["gcc", "clang"]
    opts = c.all("FLAGS_OPT")
    opt = ["-O2"] if "-O2" in opts else (opts[:1] or ["-O2"])
    flags = opt + c.all("FLAGS_WARN")[:2]
    flag_str = " ".join(flags)
    out = [c.sig]
    for comp in compilers:
        out.append(f"{comp} -O2 -pipe -c app.c -o app.o")
    for cfile in c.sample("cfiles"):
        if not cfile.endswith(".c"):
            continue
        obj = cfile[:-2] + ".o"
        out.append(" ".join(f"{compilers[0]} {flag_str} -c {cfile} -o {obj}".split()))
    return out


def _pip(c: _Ctx) -> list[str]:
    out = [
        c.sig,
        "python3 -m pip install --upgrade pip",
        "python3 -m pip install --no-cache-dir -r requirements.txt",
        "python3 -m build --wheel",
    ]
    for pkg in c.sample("pypi"):
        out.append(f"pip install {pkg}")
        out.append(f"python3 -m pip install {pkg}")
    return out


def _npm(c: _Ctx) -> list[str]:
    out = [c.sig, "npm ci", "npm run build", "npm install --no-audit --no-fund"]
    names = c.sample("COMMON_PACKAGES") or c.sample("packages")
    for pkg in names:
        out.append(f"npm install {pkg}")
    return out


def _composer(c: _Ctx) -> list[str]:
    out = [c.sig, "composer update --no-interaction", "composer dump-autoload -o"]
    for pkg in c.sample("composer"):
        out.append(f"composer require {pkg}")
    return out


def _docker(c: _Ctx) -> list[str]:
    out = [
        c.sig,
        "docker build -t image .",
        "docker buildx build --load -t image .",
        "docker image prune -f",
    ]
    for tag in c.sample("docker_tags"):
        out.append(f"docker build -t {tag} .")
        out.append(f"docker rmi {tag}")
    return out


def _terraform(c: _Ctx) -> list[str]:
    out = [
        c.sig,
        "terraform init",
        "terraform validate",
        "terraform plan -out tfplan",
        "terraform apply -auto-approve tfplan",
        "terraform fmt -recursive",
    ]
    resources = c.sample("terraform_aws_resources") or c.sample("terraform_ids")
    ids = c.all("terraform_ids")
    for res, ident in zip(resources, ids):
        out.append(f"terraform import {res} {ident}")
    return out


def _ansible(c: _Ctx) -> list[str]:
    out = [c.sig, "ansible-playbook -i inventory site.yml", "ansible-playbook site.yml --check"]
    for role in c.sample("ansible_roles"):
        out.append(f"ansible-playbook -i inventory.ini {role}.yml")
    return out


def _julia(c: _Ctx) -> list[str]:
    out = [c.sig]
    for name in c.first_col("julia"):
        out.append(f"julia --project=. -e 'using {name}'")
    return out


def _make(c: _Ctx) -> list[str]:
    out = [c.sig, "make -j 15", "make -j$(nproc) all", "make clean"]
    for arch in c.sample("ARCHES"):
        out.append(f"make ARCH={arch} defconfig")
    return out


def _wget(c: _Ctx) -> list[str]:
    out = [c.sig]
    for cfile in c.sample("cfiles"):
        out.append(f"wget -c https://example.invalid/{cfile}")
    return out


def _rkhunter(c: _Ctx) -> list[str]:
    return [c.sig, "rkhunter --check --skip-keypress --report-warnings-only", "rkhunter --update"]


def _uv(c: _Ctx) -> list[str]:
    return [c.sig, "uv sync --all-extras", "uv run python -m pytest"]


def _mkinitcpio(c: _Ctx) -> list[str]:
    out = [c.sig, "mkinitcpio -P"]
    if c.all("REQUIRED_HOOKS"):
        out.append("mkinitcpio -p linux")
    return out


def _tail(c: _Ctx) -> list[str]:
    return [c.sig, "tail -F /var/log/syslog", "tail -n 200 /var/log/auth.log"]


def _go(c: _Ctx) -> list[str]:
    return [c.sig, "go build -trimpath -ldflags=-s -w ./..."]


PROFILES = {
    "cargo": _cargo,
    "gcc": _cc,
    "cc": _cc,
    "clang": _cc,
    "pip": _pip,
    "pip3": _pip,
    "npm": _npm,
    "composer": _composer,
    "docker": _docker,
    "docker-buildx": _docker,
    "docker-compose": _docker,
    "terraform": _terraform,
    "ansible-playbook": _ansible,
    "julia": _julia,
    "make": _make,
    "wget": _wget,
    "curl": _wget,
    "rkhunter": _rkhunter,
    "uv": _uv,
    "mkinitcpio": _mkinitcpio,
    "tail": _tail,
    "go": _go,
}


def _index(assets: dict[str, Asset]) -> dict[str, list[str]]:
    """Merge every asset into a name -> items map (order-independent)."""
    merged: dict[str, list[str]] = {}
    for asset in assets.values():
        if asset.kind == "ps":
            continue
        bucket = merged.setdefault(asset.name, [])
        bucket.extend(asset.items)
    return merged


def build_ps_wordlist(assets: dict[str, Asset], sample: int = 25) -> list[str]:
    """Return a deterministic, deduped list of process command lines."""
    by_name = _index(assets)
    lines: list[str] = []

    for asset in sorted(assets.values(), key=lambda a: a.id):
        if asset.kind != "literal":
            continue
        signature = (asset.meta or {}).get("signature")
        if not signature:
            continue
        signature = signature.strip()
        if not signature or _WINDOWS.search(signature):
            continue

        program = _program(signature)
        profile = PROFILES.get(program)
        if profile is None:
            lines.append(signature)
            continue
        lines.extend(profile(_Ctx(signature, by_name, sample)))

    cleaned = []
    for line in lines:
        line = " ".join(line.split())  # collapse whitespace, trim
        if line and not _WINDOWS.search(line):
            cleaned.append(line)
    return sorted(set(cleaned))
