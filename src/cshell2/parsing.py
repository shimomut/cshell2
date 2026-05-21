"""Line tokenization and quote handling."""

import os
import shlex


def expand_vars(line: str) -> str:
    """Expand $VAR and ${VAR} in line, leaving single-quoted regions unexpanded."""
    result = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'":
            j = line.find("'", i + 1)
            if j == -1:
                result.append(line[i:])
                break
            result.append(line[i : j + 1])
            i = j + 1
        elif ch == "$":
            if i + 1 < len(line) and line[i + 1] == "{":
                end = line.find("}", i + 2)
                if end != -1:
                    name = line[i + 2 : end]
                    result.append(os.environ.get(name, ""))
                    i = end + 1
                else:
                    result.append(ch)
                    i += 1
            else:
                j = i + 1
                while j < len(line) and (line[j].isalnum() or line[j] == "_"):
                    j += 1
                if j > i + 1:
                    result.append(os.environ.get(line[i + 1 : j], ""))
                    i = j
                else:
                    result.append(ch)
                    i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def tokenize(line: str) -> list[str]:
    """Split a command line into tokens, respecting quotes.

    Returns partial tokens if the line ends mid-word (no trailing space).
    An empty line returns [].
    """
    try:
        return shlex.split(line)
    except ValueError:
        # Unclosed quote — do best-effort split
        return shlex.split(line + '"')


def split_for_completion(line: str) -> tuple[list[str], str]:
    """Split line into completed tokens and the current prefix being typed.

    If line ends with whitespace, prefix is "" (user is starting a new arg).
    Otherwise prefix is the partial last token.
    """
    if not line:
        return [], ""

    if line[-1] == " ":
        return tokenize(line), ""

    tokens = tokenize(line)
    if not tokens:
        return [], ""

    return tokens[:-1], tokens[-1]
