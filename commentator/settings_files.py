"""Small edits to config.yaml and .env that keep their comments and layout: shared by install.bat
(tools/install_setup.py) and the Windows app (commentator/tray.py and its Settings window)."""
from __future__ import annotations

import copy
import re
from typing import Any

import yaml

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./-]+", s) and yaml.safe_load(s) == s:
        return s
    return "'" + s.replace("'", "''") + "'"


def _split_value(rest: str) -> tuple[str, str]:
    """`rest` is what follows `key:` on its line: (the value, the trailing spaces and comment)."""
    body = rest.rstrip("\r\n")
    stripped = body.lstrip(" \t")
    lead = len(body) - len(stripped)
    if stripped[:1] in ("'", '"'):
        q, i = stripped[0], 1
        while i < len(stripped):
            if stripped[i] == q:
                if q == "'" and stripped[i + 1:i + 2] == "'":
                    i += 2
                    continue
                break
            i += 2 if (q == '"' and stripped[i] == "\\") else 1
        end = lead + i + 1
    else:
        m = re.search(r"(^|\s)#", stripped)
        end = lead + (m.start() if m else len(stripped))
    value = body[:end].strip()
    return value, body[len(body[:end].rstrip(" \t")):] if value else body


def _find(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    pat = re.compile(rf"^ {{{indent}}}{re.escape(key)}:(\s|$)")
    for i in range(start, end):
        if _is_content(lines[i]) and _indent(lines[i]) == indent and pat.match(lines[i]):
            return i
    return None


def _commented(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    pat = re.compile(rf"^ {{{indent}}}# ?{re.escape(key)}:(\s|$)")
    stop = end
    while stop < len(lines) and lines[stop].strip().startswith("#") and _indent(lines[stop]) >= indent:
        stop += 1   # the comments just after the block belong to it too
    for i in range(start, stop):
        if pat.match(lines[i]):
            return i
    return None


def _block_end(lines: list[str], i: int) -> int:
    """The line after the block that line i's key opens (its deeper lines, and `- ` items at its own
    indent), not counting comments and blank lines after the block's last line."""
    ind, last = _indent(lines[i]), i
    for j in range(i + 1, len(lines)):
        if not _is_content(lines[j]):
            continue
        if _indent(lines[j]) > ind or (_indent(lines[j]) == ind and lines[j].lstrip().startswith("- ")):
            last = j
        else:
            break
    return last + 1


def _child_indent(lines: list[str], i: int, end: int) -> int:
    for j in range(i + 1, end):
        if _is_content(lines[j]):
            return _indent(lines[j])
    return _indent(lines[i]) + 2


def set_yaml(text: str, keys: tuple[str, ...] | str, value: Any) -> str:
    """Set a scalar or a list of scalars at `keys` (e.g. ("source", "connector_log")) in YAML text,
    keeping every other line, comment and spacing as it is. Missing keys are added at the end of
    their block. Raises ValueError if the result wouldn't read back as `value`."""
    keys = (keys,) if isinstance(keys, str) else tuple(keys)
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    text = text[len(bom):]
    try:
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"the file doesn't read as YAML: {e}") from e
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(keys):
        last = depth == len(keys) - 1
        i = _find(lines, start, end, indent, key)
        if i is None and last:
            # A commented-out example of the key (`# league_file: data/league.json`) becomes the key.
            i = _commented(lines, start, end, indent, key)
            if i is not None:
                lines[i] = " " * indent + lines[i].lstrip()[1:].lstrip(" ")
        if i is None:
            # Add the missing keys after the parent block's last line.
            at = end
            while at > start and not _is_content(lines[at - 1]):
                at -= 1
            new = [" " * (indent + 2 * n) + k + ":" + nl for n, k in enumerate(keys[depth:-1])]
            lines[at:at] = new + _value_lines(" " * (indent + 2 * len(new)), keys[-1], value, "", nl)
            break
        if not last:
            value_here, _ = _split_value(lines[i][_indent(lines[i]) + len(key) + 1:])
            if value_here:
                raise ValueError(f"{'.'.join(keys[:depth + 1])} is written on one line; edit it by hand")
            start, end = i + 1, _block_end(lines, i)
            indent = _child_indent(lines, i, end)
            continue
        ind = _indent(lines[i])
        _, tail = _split_value(lines[i][ind + len(key) + 1:])
        column = len(lines[i].rstrip("\r\n")) - len(tail.lstrip(" \t")) if tail.strip() else None
        # Drop the old value's own lines (a list or block under the key), keeping comments among them.
        stop = _block_end(lines, i)
        item_indent = _child_indent(lines, i, stop) if stop > i + 1 else ind + 2
        kept = [ln for ln in lines[i + 1:stop] if not _is_content(ln)]
        lines[i:stop] = _value_lines(" " * ind, key, value, tail, nl, item_indent, column) + kept
        break
    out = "".join(lines)
    # Only the one value may change: anything else (a key written in a way this doesn't follow, so a
    # second copy of its section was added) means the edit went wrong.
    expected = copy.deepcopy(before) if isinstance(before, dict) else {}
    node = expected
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = list(value) if isinstance(value, (list, tuple)) else value
    try:
        after = yaml.safe_load(out)
    except yaml.YAMLError as e:
        raise ValueError(f"couldn't set {'.'.join(keys)}: {e}") from e
    if after != expected:
        raise ValueError(f"couldn't set {'.'.join(keys)} in this file; edit it by hand")
    return bom + out


def _value_lines(pad: str, key: str, value: Any, tail: str, nl: str, item_indent: int | None = None,
                 column: int | None = None) -> list[str]:
    """The key's line (and list items), with its trailing comment kept in its old column if it fits."""
    def with_tail(head: str) -> str:
        if not tail.strip():
            return head
        gap = column - len(head) if column is not None else 0
        return head + " " * max(gap, 2 if column is not None else 1) + tail.lstrip(" \t")
    if isinstance(value, (list, tuple)):
        items = " " * (item_indent if item_indent is not None else len(pad) + 2)
        if not value:
            return [with_tail(f"{pad}{key}: []") + nl]
        return [with_tail(f"{pad}{key}:") + nl] + [f"{items}- {_scalar(v)}{nl}" for v in value]
    v = _scalar(value)
    return [with_tail(f"{pad}{key}:{' ' + v if v else ''}") + nl]


def set_commentary(config_text: str, enabled: bool) -> str:
    """Flip `commentary: enabled:` in config.yaml's text, keeping its comments."""
    return set_yaml(config_text, ("commentary", "enabled"), bool(enabled))


def _env_line(key: str) -> re.Pattern:
    # As commentator/dotenv.py reads it: spaces around the key, a byte-order mark on the first line.
    return re.compile(rf"^\ufeff?[ \t]*{re.escape(key)}[ \t]*=(.*)$", re.M)


def env_value(env_text: str, key: str) -> str:
    """The key's value as the booth sees it (the last non-empty line for it wins, quotes stripped)."""
    found = [v.strip().strip('"').strip("'") for v in _env_line(key).findall(env_text)]
    found = [v for v in found if v]
    return found[-1] if found else ""


def set_env(env_text: str, key: str, value: str) -> str:
    value = value.replace("\r", "").replace("\n", "")
    pattern = _env_line(key)
    if pattern.search(env_text):
        bom = "\ufeff" if env_text.startswith("\ufeff") else ""
        return bom + pattern.sub(lambda _: f"{key}={value}", env_text[len(bom):])
    return env_text.rstrip("\n") + f"\n{key}={value}\n"
