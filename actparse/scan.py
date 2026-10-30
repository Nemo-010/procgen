"""Source scanning primitives: string literals and flat array literals.

Everything here is syntax-driven, never path-driven. A module can be renamed,
moved or have its variables renamed and these scanners keep working: they look
at the shape of the code, not at where it lives.
"""

from __future__ import annotations

import re

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "`": "`",
    "/": "/",
}


def _unescape(raw: str) -> str:
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            e = raw[i + 1]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                i += 2
                continue
            if e == "x" and i + 3 < n:
                try:
                    out.append(chr(int(raw[i + 2 : i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if e == "u":
                if i + 2 < n and raw[i + 2] == "{":
                    end = raw.find("}", i + 3)
                    if end != -1:
                        try:
                            out.append(chr(int(raw[i + 3 : end], 16)))
                            i = end + 1
                            continue
                        except ValueError:
                            pass
                elif i + 5 < n:
                    try:
                        out.append(chr(int(raw[i + 2 : i + 6], 16)))
                        i += 6
                        continue
                    except ValueError:
                        pass
            out.append(e)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scan_quoted(text: str, i: int, quote: str, allow_newline: bool = False):
    """text[i] == quote. Return (decoded_value, end_after_closing) or None."""
    n = len(text)
    j = i + 1
    start = j
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return _unescape(text[start:j]), j + 1
        if c == "\n" and not allow_newline:
            return None
        j += 1
    return None


def iter_literals(
    text: str,
    *,
    single: bool = False,
    backtick: bool = False,
    raw_strings: bool = False,
):
    """Yield (value, start, end) for every string literal, skipping comments."""
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if raw_strings and c == "r" and i + 1 < n and text[i + 1] in '"#':
            j = i + 1
            hashes = 0
            while j < n and text[j] == "#":
                hashes += 1
                j += 1
            if j < n and text[j] == '"':
                closing = '"' + "#" * hashes
                k = text.find(closing, j + 1)
                if k != -1:
                    yield text[j + 1 : k], i, k + len(closing)
                    i = k + len(closing)
                    continue
            i += 1
            continue
        if c == '"':
            res = _scan_quoted(text, i, '"')
            if res:
                val, end = res
                yield val, i, end
                i = end
                continue
            i += 1
            continue
        if single and c == "'":
            res = _scan_quoted(text, i, "'")
            if res:
                val, end = res
                yield val, i, end
                i = end
                continue
            i += 1
            continue
        if backtick and c == "`":
            res = _scan_quoted(text, i, "`", allow_newline=True)
            if res:
                val, end = res
                yield val, i, end
                i = end
                continue
            i += 1
            continue
        i += 1


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def extract_flat_array(
    text: str,
    open_idx: int,
    *,
    single: bool = False,
    backtick: bool = False,
    raw_strings: bool = False,
):
    """text[open_idx] == '['. Return ('str'|'num', items) or None.

    Only flat arrays of scalars qualify; anything containing identifiers,
    spreads, nested arrays or expressions is rejected.
    """
    n = len(text)
    i = open_idx + 1
    items: list[str] = []
    kind: str | None = None
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "]":
            return (kind, items) if kind else None
        if c == ",":
            i += 1
            continue

        lit = None
        if c == '"':
            lit = _scan_quoted(text, i, '"')
        elif single and c == "'":
            lit = _scan_quoted(text, i, "'")
        elif backtick and c == "`":
            lit = _scan_quoted(text, i, "`", allow_newline=True)
        elif raw_strings and c == "r" and i + 1 < n and text[i + 1] in '"#':
            j = i + 1
            hashes = 0
            while j < n and text[j] == "#":
                hashes += 1
                j += 1
            if j < n and text[j] == '"':
                closing = '"' + "#" * hashes
                k = text.find(closing, j + 1)
                if k != -1:
                    lit = (text[j + 1 : k], k + len(closing))

        if lit:
            if kind == "num":
                return None
            kind = "str"
            val, end = lit
            items.append(val)
            i = end
            continue

        m = _NUM.match(text, i)
        if m and kind != "str":
            kind = "num"
            items.append(m.group(0))
            i = m.end()
            continue
        return None
    return None


# `static NAME: &[&str] = &[`  /  `const NAME: [&str; 3] = [`
RUST_DECL = re.compile(
    r"\b(?:static|const)\s+([A-Za-z_]\w*)\s*:\s*&?\s*\[[^\]]*\]\s*=\s*&?\s*\[",
    re.S,
)

# `const NAME = [`  /  `export const NAME: string[] = [`
TS_DECL = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;\n]+)?=\s*\["
)
