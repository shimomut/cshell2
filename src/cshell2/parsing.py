"""Line tokenization and quote handling."""

import shlex


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
