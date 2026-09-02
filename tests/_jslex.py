"""Minimal JS lexer good enough to strip strings, comments and regex literals.

There is no JS runtime in this environment, so the frontend gets no syntax check
at all unless we build one.  A full parser is overkill; what we actually need is
to know which characters are *code* so the guard tests can reason about brackets
and stray punctuation without being fooled by Chinese text inside string
literals (the UI is full of it).

The one genuinely tricky part is `/`: it starts a regex in expression position
and is division otherwise.  We track the previous significant character and use
the standard heuristic.
"""

# After these, a `/` can only begin a regex, never division.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>") | {""}
# Keywords that put us in expression position even though they end in a letter.
_REGEX_KEYWORDS = ("return", "typeof", "instanceof", "in", "of", "new", "delete",
                   "void", "case", "do", "else", "yield", "await")


def strip_js(src):
    """Return src with every string/template/comment/regex body blanked out.

    Blanked characters become spaces so that all offsets and line numbers stay
    identical to the original, which lets callers report useful line numbers.
    """
    out = []
    i = 0
    n = len(src)
    prev = ""          # previous significant (non-space) code character
    prev_word = ""     # previous identifier, for the keyword heuristic
    # Template literals can nest via ${...}, so a stack is required.
    while i < n:
        c = src[i]

        # --- comments -------------------------------------------------------
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue

        # --- quoted strings -------------------------------------------------
        if c in "'\"":
            quote = c
            out.append(" ")
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if src[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            prev, prev_word = "x", ""
            continue

        # --- template literals ---------------------------------------------
        if c == "`":
            out.append(" ")
            i += 1
            depth = 0
            while i < n:
                ch = src[i]
                if ch == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if ch == "$" and i + 1 < n and src[i + 1] == "{":
                    # Interpolations hold real code: keep them verbatim so
                    # bracket balance and stray punctuation are still checked.
                    out.append("${")
                    i += 2
                    depth += 1
                    inner_depth = 1
                    while i < n and inner_depth:
                        c2 = src[i]
                        if c2 == "{":
                            inner_depth += 1
                        elif c2 == "}":
                            inner_depth -= 1
                            if not inner_depth:
                                out.append("}")
                                i += 1
                                depth -= 1
                                break
                        elif c2 in "'\"`":
                            # Nested string inside the interpolation.
                            q = c2
                            out.append(" ")
                            i += 1
                            while i < n and src[i] != q:
                                if src[i] == "\\":
                                    out.append("  ")
                                    i += 2
                                    continue
                                out.append("\n" if src[i] == "\n" else " ")
                                i += 1
                            out.append(" ")
                            i += 1
                            continue
                        out.append(c2)
                        i += 1
                    continue
                if ch == "`":
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if ch == "\n" else " ")
                i += 1
            prev, prev_word = "x", ""
            continue

        # --- regex literals -------------------------------------------------
        if c == "/" and (prev in _REGEX_PRECEDERS or prev_word in _REGEX_KEYWORDS):
            out.append(" ")
            i += 1
            in_class = False
            while i < n:
                ch = src[i]
                if ch == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    out.append(" ")
                    i += 1
                    # flags
                    while i < n and src[i].isalpha():
                        out.append(" ")
                        i += 1
                    break
                elif ch == "\n":
                    break  # unterminated; let the bracket check complain
                out.append(" ")
                i += 1
            prev, prev_word = "x", ""
            continue

        # --- ordinary code --------------------------------------------------
        out.append(c)
        if not c.isspace():
            if c.isalnum() or c in "_$":
                prev_word += c
            else:
                prev_word = ""
            prev = c
        i += 1

    return "".join(out)
