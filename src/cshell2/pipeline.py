"""Quote-aware shell operator parser: pipes, redirections, sequencing."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field


@dataclass
class Redirect:
    kind: str    # ">", ">>", "<", "2>", "2>>"  or "2>&1"
    target: str  # filename, or "1" for 2>&1


@dataclass
class Stage:
    """One command in a pipeline."""
    text: str
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Pipeline:
    stages: list[Stage]


@dataclass
class Sequence:
    """Pipelines joined by ;, &&, or ||.  operator is None for the first item."""
    items: list[tuple[str | None, Pipeline]]


# ---------------------------------------------------------------------------
# Quote-aware operator splitter
# ---------------------------------------------------------------------------

def _split_on_operators(text: str, operators: list[str]) -> list[tuple[str | None, str]]:
    """Split *text* on any token in *operators*, respecting single/double quotes.

    Returns [(op_before | None, segment_text), ...].
    Operators are matched longest-first.
    """
    ops = sorted(operators, key=len, reverse=True)
    segments: list[tuple[str | None, str]] = []
    current: list[str] = []
    pending_op: str | None = None
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # Quoted region — pass through verbatim
        if ch in ('"', "'"):
            quote = ch
            current.append(ch)
            i += 1
            while i < n:
                c = text[i]
                current.append(c)
                i += 1
                if c == quote:
                    break
                if quote == '"' and c == '\\' and i < n:
                    current.append(text[i])
                    i += 1
            continue

        # Backslash outside quotes
        if ch == '\\' and i + 1 < n:
            current.append(ch)
            current.append(text[i + 1])
            i += 2
            continue

        # Try operators longest-first
        matched: str | None = None
        for op in ops:
            if text[i:i + len(op)] == op:
                matched = op
                break

        if matched:
            segments.append((pending_op, "".join(current)))
            current = []
            pending_op = matched
            i += len(matched)
        else:
            current.append(ch)
            i += 1

    segments.append((pending_op, "".join(current)))
    return segments


# ---------------------------------------------------------------------------
# Redirect extraction
# ---------------------------------------------------------------------------

_REDIRECT_OPS = ["2>&1", "2>>", "2>", ">>", ">", "<"]


def _extract_redirects(text: str) -> tuple[str, list[Redirect]]:
    """Remove redirect tokens (and their targets) from *text*.

    Returns (cleaned_text, list_of_Redirect).
    """
    redirects: list[Redirect] = []
    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == ' ':
            result.append(ch)
            i += 1
            continue

        # Quoted token — not a redirect op
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == '\\' and quote == '"' and j + 1 < n:
                    j += 2
                else:
                    j += 1
            result.append(text[i:j])
            i = j
            continue

        # Try redirect operators
        matched_op: str | None = None
        for op in _REDIRECT_OPS:
            if text[i:i + len(op)] == op:
                matched_op = op
                break

        if matched_op:
            i += len(matched_op)
            while i < n and text[i] == ' ':
                i += 1
            if matched_op == "2>&1":
                redirects.append(Redirect(kind="2>&1", target="1"))
                continue
            # Read the target filename
            target: list[str] = []
            if i < n and text[i] in ('"', "'"):
                quote = text[i]
                i += 1
                while i < n and text[i] != quote:
                    if text[i] == '\\' and quote == '"' and i + 1 < n:
                        target.append(text[i + 1])
                        i += 2
                    else:
                        target.append(text[i])
                        i += 1
                i += 1  # closing quote
            else:
                while i < n and text[i] not in (' ', '\t'):
                    target.append(text[i])
                    i += 1
            if target:
                redirects.append(Redirect(kind=matched_op, target="".join(target)))
            continue

        result.append(ch)
        i += 1

    return "".join(result), redirects


# ---------------------------------------------------------------------------
# Glob expansion
# ---------------------------------------------------------------------------

def expand_globs(tokens: list[str]) -> list[str]:
    """Expand glob patterns; non-matching patterns are kept as-is."""
    result: list[str] = []
    for token in tokens:
        if any(c in token for c in ('*', '?', '[')):
            recursive = '**' in token
            expanded = glob.glob(os.path.expanduser(token), recursive=recursive)
            if expanded:
                result.extend(sorted(expanded))
                continue
        result.append(token)
    return result


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------

def parse_line(line: str) -> Sequence:
    """Parse a raw shell line into a Sequence of Pipelines."""
    seq_parts = _split_on_operators(line, [";", "&&", "||"])
    items: list[tuple[str | None, Pipeline]] = []

    for op, part in seq_parts:
        part = part.strip()
        if not part:
            continue
        pipe_parts = _split_on_operators(part, ["|"])
        stages: list[Stage] = []
        for _, stage_text in pipe_parts:
            stage_text = stage_text.strip()
            if not stage_text:
                continue
            cleaned, redirects = _extract_redirects(stage_text)
            stages.append(Stage(text=cleaned.strip(), redirects=redirects))
        if stages:
            items.append((op, Pipeline(stages=stages)))

    return Sequence(items=items)
