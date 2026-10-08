"""Grounding check for LLM-written text: every number in the output must appear in the input.

Numbers are compared after normalisation (no £, commas or trailing %; trailing zeros after a
decimal point dropped), so "£1,234.50", "1234.5" and "1234.50" are the same number. Hyphens
separate numbers, so "2026-10-06" contributes 2026, 10 and 6 (grounding "6 Oct 2026").
"""
from __future__ import annotations

import re

NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(token: str) -> str:
    t = token.replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


def numbers(text: str) -> set[str]:
    return {_norm(m.group().rstrip(",")) for m in NUM.finditer(text)}


def ungrounded(output: str, source: str) -> list[str]:
    """Numbers in `output` that don't appear in `source` (normalised). Empty list = grounded."""
    allowed = numbers(source)
    seen, bad = set(), []
    for m in NUM.finditer(output):
        n = _norm(m.group().rstrip(","))
        if n not in allowed and n not in seen:
            bad.append(m.group().rstrip(","))
            seen.add(n)
    return bad
