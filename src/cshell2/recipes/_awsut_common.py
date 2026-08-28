"""The output contract for the whole ``awsut`` command tree.

Every ``awsut`` leaf that prints a resource listing prints it the same way, and
this module is the only place that shape is defined:

* **A header line, then a blank line.**  ``N thing(s) · <scope> · region <r>`` —
  how many, of what, from where.  It is also the answer when there are none:
  :func:`print_table` prints nothing for an empty row list, so the header is
  what says "asked, and there were zero".
* **A table.**  ALL-CAPS column labels, a ``-`` rule, two spaces between
  columns, and **widths from the data** so an identifier is never truncated —
  every cell can be copied straight into the next command.
* **Notes only for facts about this output**: rows hidden by a filter, rows cut
  by ``--max``, a query that was refused.  Not a legend, and not advice — a
  sentence that reads the same on every run belongs in the command's ``help=``,
  where it is available on demand instead of costing three lines per listing.
* **Detail views** (``describe``) open with a :func:`print_labeled` block whose
  labels are the API's own field names, then use tables for the nested lists.
  Anything after that block that isn't a table gets a :func:`section` heading,
  so the eye can find where one field's worth of output ends and the next
  begins without counting blank lines.
* **Failures** read like shell errors: ``error: <message>`` on stderr, lowercase,
  the resource quoted.  :func:`guard` on the leaf turns the expected AWS and
  usage failures into that one line.
* **Timestamps** are local and second-precision (:func:`fmt_time`); the change
  lines a ``watch`` loop emits are stamped ``[HH:MM:SS]``.

It lives at the top of ``recipes/`` — above both ``awsut.py`` and the
``_awsut_sagemaker`` subpackage — and deliberately builds no AWS clients, so
either side can import it without an import cycle.  ``_awsut_sagemaker.render``
re-exports these names, since that subpackage's modules already read their
instruments from there.
"""

from __future__ import annotations

import functools
import sys

import botocore.exceptions

RESET = "\033[0m"
RED = "\033[91m"
YELLOW = "\033[93m"


# ─── errors ─────────────────────────────────────────────────────────────────

class SmError(Exception):
    """A user-facing failure — printed as ``error: <message>``, no traceback."""


def api_message(exc: botocore.exceptions.ClientError) -> str:
    return exc.response["Error"].get("Message", str(exc))


def error_code(exc: botocore.exceptions.ClientError) -> str:
    return exc.response["Error"].get("Code", "")


def guard(fn):
    """Turn the expected AWS/usage failures into one-line messages.

    A cshell2 command runs in-process, so an uncaught ``ClientError`` would
    dump a traceback into the middle of the user's session and a bare
    ``SystemExit`` could take the shell down with it.  Every leaf handler in
    the ``awsut`` tree is wrapped, so a failure reads like a shell error
    instead.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SmError as exc:
            print(f"error: {exc}", file=sys.stderr)
        except botocore.exceptions.NoCredentialsError:
            print("error: no AWS credentials found — refresh them and retry",
                  file=sys.stderr)
        except botocore.exceptions.ClientError as exc:
            print(f"error: {api_message(exc)}", file=sys.stderr)

    return wrapper


# ─── time / size formatting ─────────────────────────────────────────────────

def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_time(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def fmt_bytes(size):
    if size is None:
        return "-"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:,.1f}{unit}"
        size /= 1024


# ─── listings ───────────────────────────────────────────────────────────────

def print_header(*parts) -> None:
    """The line above a table: how many, of what, from where.

    Joined with ``·`` and followed by a blank line, so the count and the scope
    read as one fact and the table below starts clean.  Empty parts drop out,
    which lets a caller pass a conditional scope without composing the string
    itself.
    """
    print(" · ".join(str(p) for p in parts if p) + "\n")


def print_table(header, rows, note=None, colorize=None) -> None:
    """Column widths from the data, so no identifier is ever truncated.

    ``colorize(col_index, value)`` may return an ANSI colour for a cell (a
    severity column, typically).  It is applied *after* padding and only on a
    TTY, so the escape bytes can never throw the alignment off or land in a
    file.

    No rows draws no table — the header line above it already said "zero" — but
    the *note* still prints, because "everything that matched was filtered out"
    is precisely the empty listing that needs explaining.
    """
    if not rows:
        if note:
            print(note)
        return
    widths = [max([len(h)] + [len(r[i]) for r in rows])
              for i, h in enumerate(header)]
    tty = colorize is not None and sys.stdout.isatty()

    def line(cells, paint=False):
        out = []
        for i, (value, width) in enumerate(zip(cells, widths)):
            # The trailing column needs no padding.
            text = value if i == len(widths) - 1 else value.ljust(width)
            color = colorize(i, value) if paint and tty else None
            out.append(f"{color}{text}{RESET}" if color else text)
        return "  ".join(out).rstrip()

    print(line(header))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row, paint=True))
    if note:
        print(f"\n{note}")


def print_labeled(pairs) -> None:
    """``Label  value`` lines, labels padded to one width.

    The identifier block a detail view opens with: the API's own field names on
    the left, values unwrapped on the right so they can be copied out.  A pair
    whose value is empty is dropped (the caller doesn't have to branch), and a
    ``None`` in place of a pair prints a blank line — a group separator that
    still shares the one label width.
    """
    kept = [p for p in pairs if p is None or p[1] not in (None, "")]
    labels = [p[0] for p in kept if p is not None]
    if not labels:
        return
    width = max(len(label) for label in labels)
    for pair in kept:
        if pair is None:
            print()
            continue
        label, value = pair
        print(f"{label.ljust(width)}  {value}")


def section(label) -> None:
    """A named block inside one command's output.

    Two uses, one look: the repeated blocks a fan-out command emits (per node,
    per log stream, per S3 location) and the named blocks a detail view prints
    after its identifier block (``FailureReason``, ``Tags``, a document).  Both
    are "here starts a differently-shaped piece of output", which is what the
    rule marks.
    """
    print(f"--- {label} ---")
