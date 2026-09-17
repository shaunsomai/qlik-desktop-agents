"""A small JS object-literal reader for minified nebula bundles.

Regex conversion broke on the harder bundles. This parses properly and, where
a value is an expression rather than a literal (a function, an identifier,
`void 0`), reports it as UNSET so the caller can omit the key -- which is what
`undefined` means in the property definitions.
"""

UNSET = object()
WS = " \t\r\n"


class JsReader:
    def __init__(self, src, pos=0):
        self.s = src
        self.i = pos

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in WS:
            self.i += 1

    def string(self):
        quote = self.s[self.i]
        self.i += 1
        out = []
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\":
                nxt = self.s[self.i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b",
                            "f": "\f", "0": "\0"}.get(nxt, nxt))
                self.i += 2
                continue
            if ch == quote:
                self.i += 1
                return "".join(out)
            out.append(ch)
            self.i += 1
        raise ValueError("unterminated string")

    def number(self):
        start = self.i
        if self.s[self.i] in "+-":
            self.i += 1
        while self.i < len(self.s) and (self.s[self.i].isdigit()
                                        or self.s[self.i] in ".eE+-"):
            if self.s[self.i] in "+-" and self.s[self.i - 1] not in "eE":
                break
            self.i += 1
        text = self.s[start:self.i]
        try:
            return int(text)
        except ValueError:
            return float(text)

    def skip_expression(self):
        """Consume a non-literal value up to the next , or } or ] at depth 0."""
        depth = 0
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in "\"'`":
                self.string()
                continue
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                if depth == 0:
                    return
                depth -= 1
            elif ch == "," and depth == 0:
                return
            self.i += 1

    def value(self):
        self.ws()
        ch = self.s[self.i]
        if ch == "{":
            return self.object()
        if ch == "[":
            return self.array()
        if ch in "\"'`":
            return self.string()
        if ch.isdigit() or (ch in "+-" and self.s[self.i + 1].isdigit()):
            return self.number()
        if self.s.startswith("!0", self.i):
            self.i += 2
            return True
        if self.s.startswith("!1", self.i):
            self.i += 2
            return False
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        # void 0, undefined, identifiers, function calls -> treat as absent
        self.skip_expression()
        return UNSET

    def array(self):
        self.i += 1  # [
        out = []
        while True:
            self.ws()
            if self.s[self.i] == "]":
                self.i += 1
                return out
            v = self.value()
            if v is not UNSET:
                out.append(v)
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1

    def key(self):
        self.ws()
        ch = self.s[self.i]
        if ch in "\"'`":
            return self.string()
        if ch == "[":            # computed key - unsupported
            self.skip_expression()
            return None
        start = self.i
        while self.i < len(self.s) and (self.s[self.i].isalnum()
                                        or self.s[self.i] in "_$"):
            self.i += 1
        return self.s[start:self.i] or None

    def object(self):
        self.i += 1  # {
        out = {}
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise ValueError("unterminated object")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            if self.s[self.i] == ",":
                self.i += 1
                continue
            k = self.key()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ":":
                self.i += 1
                v = self.value()
                if k and v is not UNSET:
                    out[k] = v
            else:
                self.skip_expression()   # shorthand or method
