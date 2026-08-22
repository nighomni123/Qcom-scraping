"""
miniyaml.py — stdlib-only YAML subset parser (no pyyaml needed).

Supports everything this project's configs use:
  * nested mappings by indentation
  * sequences of scalars, inline-flow maps ({a: 1, b: 2}), and multi-line maps
      offers:
        - platform: blinkit
          code: WELCOME50
          value: 50
  * inline flow sequences [a, b, c]
  * comments (# ...) outside quotes, quoted strings, ints/floats/bools/null

NOT a general YAML implementation — deliberately small and predictable.
"""
from __future__ import annotations


def _strip_comment(line):
    out, q = [], None
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
            out.append(ch)
        elif ch == '#':
            break
        else:
            out.append(ch)
    return ''.join(out)


def _scalar(s):
    s = s.strip()
    if s.startswith('{') and s.endswith('}'):
        inner = s[1:-1].strip()
        d = {}
        for part in _split_flow(inner) if inner else []:
            if ':' in part:
                k, _, v = part.partition(':')
                d[k.strip()] = _scalar(v)
        return d
    if s.startswith('[') and s.endswith(']'):
        inner = s[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ('true', 'yes'):
        return True
    if low in ('false', 'no'):
        return False
    if low in ('null', 'none', '~', ''):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _split_flow(s):
    out, buf, depth, q = [], '', 0, None
    for ch in s:
        if q:
            buf += ch
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
            buf += ch
        elif ch in '[{':
            depth += 1
            buf += ch
        elif ch in ']}':
            depth -= 1
            buf += ch
        elif ch == ',' and depth == 0:
            out.append(buf.strip())
            buf = ''
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def load(path):
    with open(path) as f:
        return loads(f.read())


def loads(text):
    lines = [_strip_comment(l).rstrip() for l in text.splitlines()]
    obj, _ = _parse_block(lines, 0, 0)
    return obj or {}


def _next_content(lines, i):
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def _indent_of(line):
    return len(line) - len(line.lstrip(' '))


def _parse_block(lines, i, min_indent):
    """Parse a mapping or sequence whose first content line has indent >= min_indent."""
    i = _next_content(lines, i)
    if i >= len(lines):
        return {}, i
    ind = _indent_of(lines[i])
    if ind < min_indent:
        return {}, i
    if lines[i].lstrip().startswith('- '):
        return _parse_seq(lines, i, ind)
    return _parse_map(lines, i, ind)


def _parse_map(lines, i, indent):
    d = {}
    while True:
        i = _next_content(lines, i)
        if i >= len(lines):
            return d, i
        line = lines[i]
        ind = _indent_of(line)
        if ind < indent:
            return d, i
        st = line.strip()
        if st.startswith('- '):
            return d, i  # a sequence at this level belongs to the PARENT key
        if ':' not in st:
            i += 1
            continue
        key, _, val = st.partition(':')
        key = key.strip().strip('"').strip("'")
        val = val.strip()
        if val:
            d[key] = _scalar(val)
            i += 1
        else:
            child, i2 = _parse_block(lines, i + 1, indent + 1)
            d[key] = child
            i = i2


def _parse_seq(lines, i, indent):
    out = []
    while True:
        i = _next_content(lines, i)
        if i >= len(lines):
            return out, i
        line = lines[i]
        ind = _indent_of(line)
        if ind != indent or not line.lstrip().startswith('- '):
            if ind < indent:
                return out, i
            if ind == indent:
                return out, i  # back to map keys at same level
            return out, i
        item = line.strip()[2:].strip()
        if not item:
            # bare '-': nested block follows at deeper indent
            child, i2 = _parse_block(lines, i + 1, indent + 1)
            out.append(child)
            i = i2
        elif ':' in item and not item.startswith('{') and not item.startswith('['):
            # first key of an inline-started multi-line mapping
            d = {}
            k, _, v = item.partition(':')
            v = v.strip()
            if v:
                d[k.strip()] = _scalar(v)
            # absorb following lines deeper than the '-' indent
            i += 1
            while True:
                j = _next_content(lines, i)
                if j >= len(lines):
                    break
                l2 = lines[j]
                ind2 = _indent_of(l2)
                if ind2 <= indent or l2.strip().startswith('- '):
                    i = j
                    break
                k2, _, v2 = l2.strip().partition(':')
                d[k2.strip()] = _scalar(v2.strip())
                i = j + 1
            out.append(d)
        else:
            out.append(_scalar(item))
            i += 1
