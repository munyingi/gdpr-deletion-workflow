"""Free-text redaction, by known plaintext identifiers or by keyed hash."""
from __future__ import annotations

import re

from .config import subject_hash

MASK = "[REDACTED]"
_UP = "A-ZÅÄÖ"
_LO = "a-zåäöéü"
_NAME = rf"[{_UP}][{_LO}]+(?:-[{_UP}][{_LO}]+)?"
CANDIDATE_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),            # emails
    re.compile(r"\+358 ?\d{2} ?\d{3} ?\d{3,4}"),        # Finnish mobile numbers
    re.compile(rf"\b{_NAME} {_NAME}\b"),               # "First Last" bigrams
    re.compile(r"\bC\d{5}\b"),                          # customer ids
]


def redact_known(text: str | None, identifiers: list[str]) -> tuple[str | None, bool]:
    """Replace every known identifier (longest first, case-insensitive)."""
    if not text:
        return text, False
    out = text
    for ident in sorted(identifiers, key=len, reverse=True):
        out = re.sub(re.escape(ident), MASK, out, flags=re.IGNORECASE)
    return out, out != text


def redact_by_hash(text: str | None, hashes: set[str]) -> tuple[str | None, bool]:
    """Used on restore and re-ingest, where only hashes are available:
    find identifier-shaped tokens, hash them, redact the ones on the list."""
    if not text or not hashes:
        return text, False
    out = text
    for pat in CANDIDATE_PATTERNS:
        for m in set(pat.findall(out)):
            if subject_hash(m) in hashes:
                out = out.replace(m, MASK)
    return out, out != text


def sql_regex(identifiers: list[str]) -> str:
    """RE2 alternation matching any identifier, case-insensitive."""
    meta = set(r"\.^$|?*+()[]{}")
    parts = ["".join("\\" + ch if ch in meta else ch for ch in i) for i in identifiers]
    return "(?i)(" + "|".join(parts) + ")"
