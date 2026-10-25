import hashlib
import math
import numpy as np
import os
import re

from .common import (
    MME_CORE_VERSION,
)


class FxError(Exception):
    pass


class FxUnsupported(FxError):
    pass


FX_COMMENT = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)


FX_PP_TOKEN = re.compile(
    r'"(?:[^"\\]|\\.)*"|[A-Za-z_]\w*|\d+\.?\d*(?:[eE][+-]?\d+)?[fFhHuUlL]*|\.\d+(?:[eE][+-]?\d+)?[fFhH]?|##|#|\s+|.',
    re.S,
)


FX_IF_SAFE = re.compile(r"^[\s\d()+\-*/%<>=!&|^~.xXa-fA-F]*$")


FX_TOKEN = re.compile(
    r"""
 (?P<ws>\s+)
|(?P<str>"(?:[^"\\]|\\.)*")
|(?P<num>0[xX][0-9a-fA-F]+[uUlL]*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFhHlLuU]*)
|(?P<id>[A-Za-z_]\w*)
|(?P<op><<=|>>=|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|==|!=|<=|>=|&&|\|\||<<|>>|::|[-+*/%<>=!&|^~?:;,.()\[\]{}#@\\$`'])
""",
    re.X | re.S,
)


FX_SCALARS = {
    "float": "f",
    "half": "f",
    "double": "f",
    "fixed": "f",
    "min16float": "f",
    "min10float": "f",
    "int": "i",
    "uint": "i",
    "dword": "i",
    "min16int": "i",
    "min12int": "i",
    "min16uint": "i",
    "bool": "b",
}


FX_TYPE_RE = re.compile(
    r"(float|half|double|fixed|min16float|min10float|int|uint|dword|min16int|min12int|min16uint|bool)([1-4])?(?:x([1-4]))?$"
)


FX_QUALIFIERS = {
    "static",
    "uniform",
    "const",
    "shared",
    "extern",
    "volatile",
    "inline",
    "row_major",
    "column_major",
    "precise",
    "groupshared",
    "nointerpolation",
    "linear",
    "centroid",
    "noperspective",
    "sample",
    "in",
    "out",
    "inout",
    "snorm",
    "unorm",
}


FX_SPECIAL_TYPES = (
    "texture",
    "texture1d",
    "texture2d",
    "texture3d",
    "texturecube",
    "sampler",
    "sampler1d",
    "sampler2d",
    "sampler3d",
    "samplercube",
    "sampler_state",
    "samplerstate",
    "string",
    "void",
    "vector",
    "matrix",
    "pixelshader",
    "vertexshader",
)


FX_BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    ">": 7,
    "<=": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}


FX_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


FX_SWIZZLE = {"x": 0, "y": 1, "z": 2, "w": 3, "r": 0, "g": 1, "b": 2, "a": 3}


FX_LOOP_LIMIT = 4096


FX_CALL_DEPTH = 48


def fx_read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp932", errors="replace")


def fx_strip_comments(text):
    def replace(match):
        found = match.group(0)
        if found.startswith('"'):
            return found
        if found.startswith("/*"):
            return "\n" * found.count("\n") or " "
        return ""

    return FX_COMMENT.sub(replace, text)


class FxPreprocessor:
    def __init__(self, root_dir, defines=None):
        self.root_dir = root_dir
        self.macros = {}
        self.files = []
        self.depth = 0
        for name, value in (defines or {}).items():
            self.macros[name] = (None, str(value))

    def run_file(self, path):
        self.files.append(os.path.abspath(path))
        return self.run_text(fx_read_text(path), os.path.dirname(os.path.abspath(path)))

    def resolve_include(self, name, base_dir):
        name = name.replace("\\", "/")
        for folder in (base_dir, self.root_dir):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
            folder_path = os.path.dirname(candidate)
            wanted = os.path.basename(candidate).lower()
            if os.path.isdir(folder_path):
                for entry in os.listdir(folder_path):
                    if entry.lower() == wanted:
                        return os.path.join(folder_path, entry)
        return None

    def run_text(self, text, base_dir):
        text = fx_strip_comments(text)
        text = text.replace("\\\r\n", "").replace("\\\n", "").replace("\r", "")
        lines = text.split("\n")
        out = []
        stack = []
        for line in lines:
            stripped = line.strip()
            active = stack[-1][2] if stack else True
            if stripped.startswith("#"):
                directive = re.match(r"#\s*(\w+)\s*(.*)", stripped, re.S)
                out.append("")
                if not directive:
                    continue
                name, rest = directive.group(1), directive.group(2).strip()
                if name in ("if", "ifdef", "ifndef"):
                    if not active:
                        condition = False
                    elif name == "ifdef":
                        condition = bool(rest) and rest.split()[0] in self.macros
                    elif name == "ifndef":
                        condition = not (bool(rest) and rest.split()[0] in self.macros)
                    else:
                        condition = self.eval_condition(rest)
                    stack.append([active, condition, active and condition])
                elif name == "elif":
                    if not stack:
                        continue
                    parent, taken, _current = stack[-1]
                    current = parent and not taken and self.eval_condition(rest)
                    stack[-1] = [parent, taken or current, current]
                elif name == "else":
                    if not stack:
                        continue
                    parent, taken, _current = stack[-1]
                    stack[-1] = [parent, True, parent and not taken]
                elif name == "endif":
                    if stack:
                        stack.pop()
                elif not active:
                    continue
                elif name == "define":
                    self.define(rest)
                elif name == "undef":
                    self.macros.pop(rest.split()[0] if rest else "", None)
                elif name == "include":
                    found = re.match(r'["<]([^">]+)[">]', rest)
                    if not found:
                        continue
                    path = self.resolve_include(found.group(1), base_dir)
                    if path is None:
                        raise FxError("include not found: %s" % found.group(1))
                    if self.depth > 16:
                        raise FxError("include nesting too deep")
                    self.depth += 1
                    self.files.append(os.path.abspath(path))
                    try:
                        out.append(
                            self.run_text(
                                fx_read_text(path),
                                os.path.dirname(os.path.abspath(path)),
                            )
                        )
                    finally:
                        self.depth -= 1
                continue
            out.append(self.expand(line) if active else "")
        return "\n".join(out)

    def define(self, rest):
        match = re.match(r"([A-Za-z_]\w*)(\([^)]*\))?\s*(.*)", rest, re.S)
        if not match:
            return
        params = None
        if match.group(2) is not None:
            params = [p.strip() for p in match.group(2)[1:-1].split(",") if p.strip()]
        self.macros[match.group(1)] = (params, match.group(3).strip())

    def expand(self, text, hidden=frozenset()):
        if not self.macros:
            return text
        tokens = FX_PP_TOKEN.findall(text)
        out = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if (
                token in self.macros
                and token not in hidden
                and (token[0].isalpha() or token[0] == "_")
            ):
                params, body = self.macros[token]
                if params is None:
                    out.append(self.expand(body, hidden | {token}))
                    i += 1
                    continue
                j = i + 1
                while j < len(tokens) and tokens[j].isspace():
                    j += 1
                if j < len(tokens) and tokens[j] == "(":
                    args, end = self.collect_args(tokens, j)
                    if args is not None:
                        expanded = [self.expand(a, hidden) for a in args]
                        body_text = self.substitute(body, params, args, expanded)
                        out.append(self.expand(body_text, hidden | {token}))
                        i = end
                        continue
            out.append(token)
            i += 1
        return "".join(out)

    def collect_args(self, tokens, start):
        depth = 0
        args = []
        current = []
        i = start
        while i < len(tokens):
            token = tokens[i]
            if token == "(":
                depth += 1
                if depth > 1:
                    current.append(token)
            elif token == ")":
                depth -= 1
                if depth == 0:
                    args.append("".join(current).strip())
                    return ([] if args == [""] else args), i + 1
                current.append(token)
            elif token == "," and depth == 1:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(token)
            i += 1
        return None, start

    def substitute(self, body, params, raw, expanded):
        tokens = FX_PP_TOKEN.findall(body)
        out = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "#" and i + 1 < len(tokens) and tokens[i + 1] in params:
                out.append('"%s"' % raw[params.index(tokens[i + 1])])
                i += 2
                continue
            if token == "##":
                while out and out[-1].isspace():
                    out.pop()
                i += 1
                while i < len(tokens) and tokens[i].isspace():
                    i += 1
                if i < len(tokens):
                    nxt = tokens[i]
                    out.append(raw[params.index(nxt)] if nxt in params else nxt)
                    i += 1
                continue
            if token in params:
                index = params.index(token)
                out.append(expanded[index] if index < len(expanded) else "")
            else:
                out.append(token)
            i += 1
        return "".join(out)

    def eval_condition(self, expression):
        expression = re.sub(
            r"defined\s*\(\s*(\w+)\s*\)|defined\s+(\w+)",
            lambda m: "1" if (m.group(1) or m.group(2)) in self.macros else "0",
            expression,
        )
        expression = self.expand(expression)
        expression = re.sub(r"(\d+)[uUlL]+\b", r"\1", expression)
        expression = re.sub(r"\b(?!0[xX])[A-Za-z_]\w*\b", "0", expression)
        if not FX_IF_SAFE.match(expression):
            return False
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        expression = re.sub(r"(?<![/])/(?![/])", "//", expression)
        try:
            return bool(eval(expression, {"__builtins__": {}}, {}))
        except Exception:
            return False


def fx_tokenize(text):
    tokens = []
    for match in FX_TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, match.group(0)))
    tokens.append(("eof", ""))
    return tokens


class FxStruct:
    __slots__ = ("name", "members")

    def __init__(self, name):
        self.name = name
        self.members = []


class FxParam:
    __slots__ = ("name", "type", "semantic", "quals", "default", "array")

    def __init__(self, name, type_name, semantic, quals, default, array):
        self.name = name
        self.type = type_name
        self.semantic = semantic
        self.quals = quals
        self.default = default
        self.array = array


class FxFunction:
    __slots__ = ("name", "ret", "semantic", "params", "body")

    def __init__(self, name, ret, semantic, params, body):
        self.name = name
        self.ret = ret
        self.semantic = semantic
        self.params = params
        self.body = body


class FxGlobal:
    __slots__ = (
        "name",
        "type",
        "quals",
        "semantic",
        "register",
        "ann",
        "init",
        "array",
        "order",
    )

    def __init__(
        self, name, type_name, quals, semantic, register, ann, init, array, order
    ):
        self.name = name
        self.type = type_name
        self.quals = quals
        self.semantic = semantic
        self.register = register
        self.ann = ann
        self.init = init
        self.array = array
        self.order = order


class FxPass:
    __slots__ = ("name", "ann", "states", "vs", "ps")

    def __init__(self, name, ann):
        self.name = name
        self.ann = ann
        self.states = {}
        self.vs = None
        self.ps = None


class FxTechnique:
    __slots__ = ("name", "ann", "passes")

    def __init__(self, name, ann):
        self.name = name
        self.ann = ann
        self.passes = []


class FxProgram:
    def __init__(self):
        self.structs = {}
        self.typedefs = {}
        self.functions = {}
        self.globals = {}
        self.techniques = []
        self.folder = ""
        self.files = []

    def is_type(self, name):
        return (
            name in self.structs
            or name in self.typedefs
            or name in FX_SCALARS
            or FX_TYPE_RE.match(name) is not None
            or name.lower() in FX_SPECIAL_TYPES
        )


class FxParser:
    def __init__(self, tokens, program):
        self.tokens = tokens
        self.pos = 0
        self.program = program
        self.order = 0

    def peek(self, offset=0):
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    @property
    def text(self):
        return self.tokens[self.pos][1]

    @property
    def kind(self):
        return self.tokens[self.pos][0]

    def advance(self):
        token = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return token

    def accept(self, text):
        if self.kind != "str" and self.text == text:
            self.advance()
            return True
        return False

    def expect(self, text):
        if self.kind == "str" or self.text != text:
            raise FxError(
                "expected '%s' but found '%s' (token %d)" % (text, self.text, self.pos)
            )
        return self.advance()

    def ident(self):
        if self.kind != "id":
            raise FxError(
                "expected identifier but found '%s' (token %d)" % (self.text, self.pos)
            )
        return self.advance()[1]

    def parse(self):
        while self.kind != "eof":
            if self.accept(";"):
                continue
            if self.text == "struct":
                self.parse_struct()
            elif self.text in ("technique", "technique9", "technique10", "technique11"):
                self.parse_technique()
            elif self.text == "typedef":
                self.advance()
                quals = self.parse_qualifiers()
                type_name = self.parse_type()
                name = self.ident()
                self.program.typedefs[name] = type_name
                self.expect(";")
            elif self.text == "cbuffer" or self.text == "tbuffer":
                self.advance()
                self.ident()
                if self.accept(":"):
                    self.parse_semantic_after_colon()
                self.expect("{")
                while not self.accept("}"):
                    self.parse_declaration_or_function()
            else:
                self.parse_declaration_or_function()
        return self.program

    def parse_qualifiers(self):
        quals = set()
        while self.kind == "id" and self.text in FX_QUALIFIERS:
            quals.add(self.advance()[1])
        return quals

    def parse_type(self):
        name = self.ident()
        if name == "unsigned":
            name = (
                self.ident()
                if self.kind == "id" and self.text in ("int", "short", "char")
                else "int"
            )
            return "uint"
        if name == "struct":
            return self.ident()
        lower = name.lower()
        if lower in ("vector", "matrix") and self.text == "<":
            self.advance()
            base = self.ident()
            dims = []
            while self.accept(","):
                dims.append(self.advance()[1])
            self.expect(">")
            if lower == "vector":
                return "%s%s" % (base, dims[0] if dims else "4")
            return "%s%sx%s" % (
                base,
                dims[0] if dims else "4",
                dims[1] if len(dims) > 1 else "4",
            )
        if (
            lower in ("texture1d", "texture2d", "texture3d", "texturecube", "texture")
            and self.text == "<"
        ):
            self.advance()
            depth = 1
            while depth and self.kind != "eof":
                if self.text == "<":
                    depth += 1
                elif self.text == ">":
                    depth -= 1
                self.advance()
        return self.program.typedefs.get(name, name)

    def parse_struct(self):
        self.expect("struct")
        name = self.ident()
        struct = FxStruct(name)
        self.program.structs[name] = struct
        self.expect("{")
        while not self.accept("}"):
            quals = self.parse_qualifiers()
            type_name = self.parse_type()
            while True:
                member = self.ident()
                array = None
                if self.accept("["):
                    array = self.parse_expression()
                    self.expect("]")
                semantic = None
                if self.accept(":"):
                    semantic = self.parse_semantic_after_colon()[0]
                struct.members.append((member, type_name, semantic, array))
                if not self.accept(","):
                    break
            self.expect(";")
        if self.kind == "id":
            self.ident()
        self.accept(";")

    def parse_semantic_after_colon(self):
        name = self.ident()
        if name in ("register", "packoffset") and self.accept("("):
            parts = []
            while not self.accept(")"):
                parts.append(self.advance()[1])
            return None, "".join(parts)
        return name, None

    def parse_annotations(self):
        ann = {}
        if not self.accept("<"):
            return ann
        while not self.accept(">"):
            if self.accept(";"):
                continue
            self.parse_qualifiers()
            self.parse_type()
            name = self.ident()
            if self.accept("["):
                while not self.accept("]"):
                    self.advance()
            self.expect("=")
            ann[name] = self.parse_annotation_value()
            self.expect(";")
        return ann

    def parse_annotation_value(self):
        if self.text == "{":
            return self.parse_initializer()
        return self.parse_ternary_no_gt()

    def parse_ternary_no_gt(self):
        saved = FX_BINARY_PRECEDENCE.pop(">")
        saved_ge = FX_BINARY_PRECEDENCE.pop(">=")
        try:
            return self.parse_assign()
        finally:
            FX_BINARY_PRECEDENCE[">"] = saved
            FX_BINARY_PRECEDENCE[">="] = saved_ge

    def parse_declaration_or_function(self):
        if self.text == "[":
            self.skip_attribute()
        quals = self.parse_qualifiers()
        type_name = self.parse_type()
        if self.text == "(" and type_name not in self.program.structs:
            raise FxError("unexpected '(' after %s" % type_name)
        name = self.ident()
        if self.text == "(":
            self.parse_function(type_name, name)
            return
        while True:
            self.parse_global_declarator(quals, type_name, name)
            if not self.accept(","):
                break
            name = self.ident()
        self.expect(";")

    def skip_attribute(self):
        self.expect("[")
        depth = 1
        while depth and self.kind != "eof":
            if self.text == "[":
                depth += 1
            elif self.text == "]":
                depth -= 1
            self.advance()

    def parse_global_declarator(self, quals, type_name, name):
        array = None
        if self.accept("["):
            array = self.parse_expression() if self.text != "]" else None
            self.expect("]")
        semantic = None
        register = None
        while self.accept(":"):
            sem, reg = self.parse_semantic_after_colon()
            semantic = sem or semantic
            register = reg or register
        ann = self.parse_annotations()
        init = None
        if self.accept("="):
            if self.text.lower() == "sampler_state":
                self.advance()
                init = ("sampler_state", self.parse_state_block())
            elif self.text == "compile":
                init = self.parse_compile()
            elif self.text == "{":
                init = self.parse_initializer()
            else:
                init = self.parse_assign()
        elif self.text == "{" and type_name.lower().startswith("sampler"):
            init = ("sampler_state", self.parse_state_block())
        self.program.globals[name] = FxGlobal(
            name,
            type_name,
            quals,
            semantic.upper() if semantic else None,
            register,
            ann,
            init,
            array,
            self.order,
        )
        self.order += 1

    def parse_state_block(self):
        states = {}
        self.expect("{")
        while not self.accept("}"):
            if self.accept(";"):
                continue
            key = self.advance()[1]
            if self.accept("["):
                while not self.accept("]"):
                    self.advance()
            self.expect("=")
            value = []
            depth = 0
            while self.kind != "eof" and not (self.text == ";" and depth == 0):
                if self.text in "(<{":
                    depth += 1
                elif self.text in ")>}":
                    depth -= 1
                value.append(self.advance()[1])
            self.expect(";")
            states[key.lower()] = value
        return states

    def parse_compile(self):
        self.expect("compile")
        profile = self.ident()
        call = self.parse_postfix(self.parse_primary())
        return ("compile", profile, call)

    def parse_initializer(self):
        self.expect("{")
        items = []
        while not self.accept("}"):
            if self.text == "{":
                items.append(self.parse_initializer())
            else:
                items.append(self.parse_assign())
            if not self.accept(","):
                self.expect("}")
                break
        return ("init", items)

    def parse_function(self, ret, name):
        self.expect("(")
        params = []
        if not self.accept(")"):
            if self.text == "void" and self.peek(1)[1] == ")":
                self.advance()
                self.expect(")")
            else:
                while True:
                    quals = self.parse_qualifiers()
                    type_name = self.parse_type()
                    pname = self.ident()
                    array = None
                    if self.accept("["):
                        array = self.parse_expression()
                        self.expect("]")
                    semantic = None
                    while self.accept(":"):
                        semantic = self.parse_semantic_after_colon()[0] or semantic
                    default = None
                    if self.accept("="):
                        default = self.parse_assign()
                    params.append(
                        FxParam(
                            pname,
                            type_name,
                            semantic.upper() if semantic else None,
                            quals,
                            default,
                            array,
                        )
                    )
                    if not self.accept(","):
                        break
                self.expect(")")
        semantic = None
        while self.accept(":"):
            semantic = self.parse_semantic_after_colon()[0] or semantic
        if self.accept(";"):
            return
        body = self.parse_block()
        self.program.functions.setdefault(name, []).append(
            FxFunction(name, ret, semantic.upper() if semantic else None, params, body)
        )

    def parse_technique(self):
        self.advance()
        name = self.ident() if self.kind == "id" else ""
        ann = self.parse_annotations()
        technique = FxTechnique(name, ann)
        self.expect("{")
        while not self.accept("}"):
            if self.accept(";"):
                continue
            self.expect("pass")
            pass_name = self.ident() if self.kind == "id" else ""
            pass_ann = self.parse_annotations()
            fx_pass = FxPass(pass_name, pass_ann)
            self.expect("{")
            while not self.accept("}"):
                if self.accept(";"):
                    continue
                key = self.advance()[1]
                if self.text == "(":
                    self.expect("(")
                    depth = 1
                    inner = []
                    while depth and self.kind != "eof":
                        if self.text == "(":
                            depth += 1
                        elif self.text == ")":
                            depth -= 1
                            if depth == 0:
                                self.advance()
                                break
                        inner.append(self.advance())
                    self.accept(";")
                    self.parse_set_shader(fx_pass, key, inner)
                    continue
                if self.accept("["):
                    while not self.accept("]"):
                        self.advance()
                self.expect("=")
                if self.text == "compile":
                    value = self.parse_compile()
                    self.expect(";")
                else:
                    value = []
                    while self.kind != "eof" and self.text != ";":
                        value.append(self.advance()[1])
                    self.expect(";")
                lower = key.lower()
                if lower == "vertexshader":
                    fx_pass.vs = value if isinstance(value, tuple) else None
                elif lower == "pixelshader":
                    fx_pass.ps = value if isinstance(value, tuple) else None
                else:
                    fx_pass.states[lower] = (
                        " ".join(value) if isinstance(value, list) else value
                    )
            technique.passes.append(fx_pass)
        self.program.techniques.append(technique)

    def parse_set_shader(self, fx_pass, key, inner):
        lower = key.lower()
        if lower not in ("setvertexshader", "setpixelshader"):
            return
        text = " ".join(t[1] for t in inner)
        match = re.match(r"CompileShader\s*\(\s*(\w+)\s*,\s*(.*)\)\s*$", text)
        if not match:
            return
        sub = FxParser(fx_tokenize(match.group(2)), self.program)
        call = sub.parse_postfix(sub.parse_primary())
        value = ("compile", match.group(1), call)
        if lower == "setvertexshader":
            fx_pass.vs = value
        else:
            fx_pass.ps = value

    def parse_block(self):
        self.expect("{")
        statements = []
        while not self.accept("}"):
            if self.kind == "eof":
                raise FxError("unexpected end of file in block")
            statements.append(self.parse_statement())
        return ("block", statements)

    def looks_like_declaration(self):
        if self.kind != "id":
            return False
        offset = 0
        while self.peek(offset)[0] == "id" and self.peek(offset)[1] in FX_QUALIFIERS:
            offset += 1
        kind, text = self.peek(offset)
        if kind != "id" or not self.program.is_type(text):
            return False
        nxt = self.peek(offset + 1)
        if nxt[1] == "<" and text.lower() in ("vector", "matrix"):
            return True
        return nxt[0] == "id"

    def parse_statement(self):
        text = self.text
        if self.kind == "str":
            return ("expr", self.parse_expression_statement())
        if text == "{":
            return self.parse_block()
        if text == ";":
            self.advance()
            return ("empty",)
        if text == "[":
            self.skip_attribute()
            return self.parse_statement()
        if text == "if":
            self.advance()
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            then = self.parse_statement()
            other = None
            if self.accept("else"):
                other = self.parse_statement()
            return ("if", condition, then, other)
        if text == "for":
            self.advance()
            self.expect("(")
            if self.accept(";"):
                init = ("empty",)
            elif self.looks_like_declaration():
                init = self.parse_local_declaration()
            else:
                init = ("expr", self.parse_expression())
                self.expect(";")
            condition = None if self.text == ";" else self.parse_expression()
            self.expect(";")
            step = None if self.text == ")" else self.parse_expression()
            self.expect(")")
            return ("for", init, condition, step, self.parse_statement())
        if text == "while":
            self.advance()
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            return ("for", ("empty",), condition, None, self.parse_statement())
        if text == "do":
            self.advance()
            body = self.parse_statement()
            self.expect("while")
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            self.expect(";")
            return ("do", body, condition)
        if text == "return":
            self.advance()
            value = None if self.text == ";" else self.parse_expression()
            self.expect(";")
            return ("return", value)
        if text in ("break", "continue", "discard"):
            self.advance()
            self.expect(";")
            return (text,)
        if text == "struct":
            self.parse_struct()
            return ("empty",)
        if self.looks_like_declaration():
            return self.parse_local_declaration()
        return ("expr", self.parse_expression_statement())

    def parse_expression_statement(self):
        expression = self.parse_expression()
        self.expect(";")
        return expression

    def parse_local_declaration(self):
        quals = self.parse_qualifiers()
        type_name = self.parse_type()
        declarators = []
        while True:
            name = self.ident()
            array = None
            if self.accept("["):
                array = self.parse_expression() if self.text != "]" else None
                self.expect("]")
            if self.accept(":"):
                self.parse_semantic_after_colon()
            init = None
            if self.accept("="):
                init = (
                    self.parse_initializer()
                    if self.text == "{"
                    else self.parse_assign()
                )
            declarators.append((name, array, init))
            if not self.accept(","):
                break
        self.expect(";")
        return ("decl", type_name, declarators, quals)

    def parse_expression(self):
        first = self.parse_assign()
        if self.text != ",":
            return first
        items = [first]
        while self.accept(","):
            items.append(self.parse_assign())
        return ("comma", items)

    def parse_assign(self):
        left = self.parse_ternary()
        if self.kind == "op" and self.text in FX_ASSIGN_OPS:
            op = self.advance()[1]
            return ("assign", op, left, self.parse_assign())
        return left

    def parse_ternary(self):
        condition = self.parse_binary(1)
        if self.accept("?"):
            a = self.parse_assign()
            self.expect(":")
            b = self.parse_assign()
            return ("tern", condition, a, b)
        return condition

    def parse_binary(self, minimum):
        left = self.parse_unary()
        while True:
            op = self.text
            precedence = FX_BINARY_PRECEDENCE.get(op) if self.kind == "op" else None
            if precedence is None or precedence < minimum:
                return left
            self.advance()
            right = self.parse_binary(precedence + 1)
            left = ("bin", op, left, right)

    def cast_ahead(self):
        if self.text != "(" or self.peek(1)[0] != "id":
            return None
        offset = 1
        while self.peek(offset)[0] == "id" and self.peek(offset)[1] in FX_QUALIFIERS:
            offset += 1
        name = self.peek(offset)[1]
        if not self.program.is_type(name) or name.lower() in (
            "texture",
            "sampler",
            "string",
            "void",
        ):
            return None
        if self.peek(offset + 1)[1] == ")":
            return offset + 2
        if self.peek(offset + 1)[1] == "[":
            return None
        return None

    def parse_unary(self):
        if self.kind == "op":
            op = self.text
            if op in ("-", "+", "!", "~"):
                self.advance()
                return ("un", op, self.parse_unary())
            if op in ("++", "--"):
                self.advance()
                return ("pre", op, self.parse_unary())
            skip = self.cast_ahead()
            if skip is not None:
                self.advance()
                self.parse_qualifiers()
                type_name = self.parse_type()
                self.expect(")")
                return ("cast", type_name, self.parse_unary())
        return self.parse_postfix(self.parse_primary())

    def parse_primary(self):
        kind, text = self.advance()
        if kind == "num":
            raw = text.rstrip("uUlLhH")
            if raw.lower().startswith("0x"):
                return ("num", float(int(raw, 16)), "i")
            if raw.endswith(("f", "F")):
                return ("num", float(raw[:-1]), "f")
            if "." in raw or "e" in raw or "E" in raw:
                return ("num", float(raw), "f")
            return ("num", float(int(raw)), "i")
        if kind == "str":
            parts = [text[1:-1]]
            while self.kind == "str":
                parts.append(self.advance()[1][1:-1])
            joined = "".join(parts)
            return ("str", joined.replace('\\"', '"').replace("\\\\", "\\"))
        if kind == "id":
            if text in ("true", "TRUE"):
                return ("num", 1.0, "b")
            if text in ("false", "FALSE"):
                return ("num", 0.0, "b")
            if self.text == "(" and self.program.is_type(text):
                return (
                    "ctor",
                    self.program.typedefs.get(text, text),
                    self.parse_args(),
                )
            if self.text == "<" and text.lower() in ("vector", "matrix"):
                self.pos -= 1
                type_name = self.parse_type()
                return ("ctor", type_name, self.parse_args())
            if self.text == "(":
                return ("call", text, self.parse_args())
            return ("id", text)
        if text == "(":
            inner = self.parse_expression()
            self.expect(")")
            return inner
        if text == "{":
            self.pos -= 1
            return self.parse_initializer()
        raise FxError("unexpected token '%s' (token %d)" % (text, self.pos))

    def parse_args(self):
        self.expect("(")
        args = []
        if self.accept(")"):
            return args
        while True:
            args.append(self.parse_assign())
            if self.accept(")"):
                return args
            self.expect(",")

    def parse_postfix(self, node):
        while True:
            if self.accept("."):
                name = self.ident()
                if self.text == "(":
                    node = ("mcall", node, name, self.parse_args())
                else:
                    node = ("member", node, name)
            elif self.accept("["):
                index = self.parse_expression()
                self.expect("]")
                node = ("index", node, index)
            elif self.kind == "op" and self.text in ("++", "--"):
                node = ("post", self.advance()[1], node)
            else:
                return node


def fx_parse_file(path, defines=None):
    folder = os.path.dirname(os.path.abspath(path))
    pre = FxPreprocessor(folder, defines)
    text = pre.run_file(path)
    program = FxProgram()
    program.folder = folder
    program.files = list(dict.fromkeys(pre.files))
    FxParser(fx_tokenize(text), program).parse()
    program.params = fx_find_params(program)
    return program


def fx_script_values(program, wanted):
    found = []
    for technique in program.techniques:
        script = fx_technique_script(technique, program) or ""
        for command in script.split(";"):
            if "=" not in command:
                continue
            key, value = command.split("=", 1)
            if key.strip().lower() == wanted:
                found.append(value.strip())
    return found


FX_COLOR_NAME = re.compile(
    r"(?i)colou?r|tint|rgb|^col(?![a-z])|(?<=[a-z0-9_])Col(?![a-z])"
)


def fx_find_params(program):
    loop_vars = set(fx_script_values(program, "loopgetindex"))
    params = {}
    for g in sorted(program.globals.values(), key=lambda item: item.order):
        if g.semantic or g.array is not None:
            continue
        quals = {str(q).lower() for q in (g.quals or [])}
        if quals & {"static", "const"}:
            continue
        if g.name in loop_vars or g.name in FX_FLAG_DEFAULTS or g.name == "use_texture":
            continue
        try:
            t = fx_resolve_type(g.type, program)
        except FxError:
            continue
        if not fx_is_numeric(t) or len(t[1]) > 1 or (t[1] and t[1][0] > 4):
            continue
        has_ui = any(str(k).lower().startswith("ui") for k in g.ann)
        if g.init is None and not has_ui:
            continue
        visible = fx_annotation(g, "UIVisible", program, None)
        if isinstance(visible, float) and not visible:
            continue
        size = fx_count(t[1])
        default = fx_const_value(program, g.init) if g.init is not None else None
        if isinstance(default, str):
            continue
        if default is None:
            default = [0.0]
        if not isinstance(default, list):
            default = [default]
        if len(default) == 1 and size > 1:
            default = default * size
        default = ([float(v) for v in default] + [0.0] * size)[:size]
        if t[0] == "i":
            default = [float(int(v)) for v in default]
        elif t[0] == "b":
            default = [1.0 if v else 0.0 for v in default]
        meta = {
            "type": t[0],
            "size": size,
            "default": default,
            "label": str(fx_annotation(g, "UIName", program, "") or g.name),
            "widget": str(fx_annotation(g, "UIWidget", program, "") or "").lower(),
            "help": str(fx_annotation(g, "UIHelp", program, "") or ""),
            "order": g.order,
        }
        meta["color"] = (
            t[0] == "f"
            and size in (3, 4)
            and (
                meta["widget"] == "color"
                or any(FX_COLOR_NAME.search(text) for text in (g.name, meta["label"]))
            )
        )
        for key in ("UIMin", "UIMax"):
            value = fx_annotation(g, key, program, None)
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, float):
                meta[key[2:].lower()] = value
        params[g.name] = meta
    return params


def fx_vs_globals(program, passes=None):
    names = set()
    seen = set()
    stack = []
    if passes is None:
        passes = [
            fx_pass for technique in program.techniques for fx_pass in technique.passes
        ]
    for technique in [None]:
        for fx_pass in passes:
            if fx_pass.vs is None:
                continue
            function, args = fx_entry(program, fx_pass.vs)
            if function is not None and id(function) not in seen:
                seen.add(id(function))
                stack.append(function.body)
            stack.append(args)

    def walk(node):
        if isinstance(node, tuple):
            if len(node) > 1 and node[0] == "id" and isinstance(node[1], str):
                names.add(node[1])
            if len(node) > 1 and node[0] == "call" and isinstance(node[1], str):
                for function in program.functions.get(node[1], []):
                    if id(function) not in seen:
                        seen.add(id(function))
                        stack.append(function.body)
            for item in node:
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    visited = set()
    while stack:
        walk(stack.pop())
        for name in list(names - visited):
            visited.add(name)
            g = program.globals.get(name)
            if g is not None and g.init is not None:
                stack.append(g.init)
    return names


def fx_structural_params(program):
    params = getattr(program, "params", {})
    used = fx_vs_globals(program) | set(fx_script_values(program, "loopbycount"))
    return sorted(name for name in params if name in used)


def fx_resolve_type(name, program):
    if isinstance(name, FxStruct):
        return name
    name = program.typedefs.get(name, name)
    struct = program.structs.get(name)
    if struct is not None:
        return struct
    match = FX_TYPE_RE.match(name)
    if match:
        base = FX_SCALARS[match.group(1)]
        rows, cols = match.group(2), match.group(3)
        if rows is None:
            return (base, ())
        if cols is None:
            return (base, (int(rows),))
        return (base, (int(rows), int(cols)))
    lower = name.lower()
    if lower == "vector":
        return ("f", (4,))
    if lower == "matrix":
        return ("f", (4, 4))
    if lower.startswith("texture"):
        return "texture"
    if lower.startswith("sampler"):
        return "sampler"
    if lower == "string":
        return "string"
    if lower == "void":
        return "void"
    raise FxError("unknown type %s" % name)


def fx_count(dims):
    if not dims:
        return 1
    if len(dims) == 1:
        return dims[0]
    return dims[0] * dims[1]


def fx_is_numeric(t):
    return isinstance(t, tuple) and len(t) == 2 and isinstance(t[1], tuple)


class FxValue:
    __slots__ = ("t", "c")

    def __init__(self, t, c):
        self.t = t
        self.c = c


def fx_array_type(elem, size):
    return ("array", elem, size)


def fx_is_array(t):
    return isinstance(t, tuple) and len(t) == 3 and t[0] == "array"


class FxSampler:
    __slots__ = ("name", "texture", "states", "register")

    def __init__(self, name, texture, states, register):
        self.name = name
        self.texture = texture
        self.states = states
        self.register = register

    def state(self, key, default=""):
        value = self.states.get(key)
        if value is None:
            return default
        return (" ".join(value) if isinstance(value, list) else str(value)).upper()

    def point(self):
        return any(
            self.state(k, "LINEAR") in ("NONE", "POINT")
            for k in ("magfilter", "minfilter")
        )

    def address(self):
        value = self.state("addressu", "WRAP")
        if value in ("CLAMP", "BORDER"):
            return "CLAMP"
        if value == "MIRROR":
            return "MIRROR"
        return "WRAP"


class FxNumpyBackend:
    vectorized = True
    wants_text = False

    def statement(self, text):
        pass

    def const(self, x):
        return np.float64(x)

    def known(self, x):
        if np.ndim(x) == 0:
            return float(x)
        return None

    def any_true(self, x):
        return bool(np.any(np.asarray(x) != 0))

    def add(self, a, b):
        return np.add(a, b)

    def sub(self, a, b):
        return np.subtract(a, b)

    def mul(self, a, b):
        return np.multiply(a, b)

    def div(self, a, b):
        return np.divide(a, b)

    def neg(self, a):
        return np.negative(a)

    def mod(self, a, b):
        return np.fmod(a, b)

    def idiv(self, a, b):
        return np.trunc(np.divide(a, b))

    def pow(self, a, b):
        return np.power(a, b)

    def unary(self, name, a):
        return FX_NP_UNARY[name](a)

    def atan2(self, a, b):
        return np.arctan2(a, b)

    def min(self, a, b):
        return np.minimum(a, b)

    def max(self, a, b):
        return np.maximum(a, b)

    def cmp(self, op, a, b):
        return FX_NP_CMP[op](a, b).astype(np.float64)

    def bitop(self, op, a, b):
        x = np.asarray(a).astype(np.int64)
        y = np.asarray(b).astype(np.int64)
        if op == "&":
            r = x & y
        elif op == "|":
            r = x | y
        elif op == "^":
            r = x ^ y
        elif op == "<<":
            r = x << np.clip(y, 0, 62)
        else:
            r = x >> np.clip(y, 0, 62)
        return r.astype(np.float64)

    def bitnot(self, a):
        return (~np.asarray(a).astype(np.int64)).astype(np.float64)

    def select(self, c, a, b):
        return np.where(np.asarray(c) != 0, a, b)

    def truth(self, a):
        return (np.asarray(a) != 0).astype(np.float64)

    def land(self, a, b):
        return np.multiply(self.truth(a), self.truth(b))

    def lor(self, a, b):
        return np.maximum(self.truth(a), self.truth(b))

    def lnot(self, a):
        return 1.0 - self.truth(a)

    def imod(self, a, b):
        return np.fmod(a, b)

    def enter(self, name):
        pass

    def leave(self):
        pass

    def tag(self, x, label):
        pass


FX_NP_UNARY = {
    "abs": np.abs,
    "sign": np.sign,
    "floor": np.floor,
    "ceil": np.ceil,
    "trunc": np.trunc,
    "round": np.round,
    "frac": lambda x: x - np.floor(x),
    "sqrt": lambda x: np.sqrt(x),
    "rsqrt": lambda x: 1.0 / np.sqrt(x),
    "exp": np.exp,
    "exp2": np.exp2,
    "log": np.log,
    "log2": np.log2,
    "log10": np.log10,
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": lambda x: np.arcsin(np.clip(x, -1, 1)),
    "acos": lambda x: np.arccos(np.clip(x, -1, 1)),
    "atan": np.arctan,
    "sinh": np.sinh,
    "cosh": np.cosh,
    "tanh": np.tanh,
}


FX_NP_CMP = {
    "<": np.less,
    "<=": np.less_equal,
    ">": np.greater,
    ">=": np.greater_equal,
    "==": np.equal,
    "!=": np.not_equal,
}


class FxFrame:
    __slots__ = ("function", "scopes", "ret", "done", "loops")

    def __init__(self, function):
        self.function = function
        self.scopes = [{}]
        self.ret = None
        self.done = 0.0
        self.loops = []


class FxLoop:
    __slots__ = ("brk", "cont")

    def __init__(self):
        self.brk = 0.0
        self.cont = 0.0


FX_TEXT_STATEMENTS = ("expr", "decl", "return", "if", "for", "do")


class FxExec:
    def __init__(self, program, backend, env):
        self.program = program
        self.B = backend
        self.env = env
        self.cache = {}
        self.overrides = {}
        self.frames = []
        self.mask = 1.0
        self.discard = 0.0
        self.depth = 0
        self.evaluating = set()

    def zero(self, t):
        B = self.B
        if fx_is_numeric(t):
            return FxValue(t, [B.const(0.0)] * fx_count(t[1]))
        if isinstance(t, FxStruct):
            members = {}
            for name, type_name, _sem, array in t.members:
                mt = fx_resolve_type(type_name, self.program)
                if array is not None:
                    size = int(self.const_int(array))
                    members[name] = FxValue(
                        fx_array_type(mt, size), [self.zero(mt) for _ in range(size)]
                    )
                else:
                    members[name] = self.zero(mt)
            return FxValue(t, members)
        if fx_is_array(t):
            return FxValue(t, [self.zero(t[1]) for _ in range(t[2])])
        return FxValue(t, None)

    def const_int(self, expr):
        value = self.eval(expr)
        known = self.B.known(value.c[0])
        if known is None:
            raise FxUnsupported("array size is not constant")
        return int(known)

    def scalar(self, x, base="f"):
        return FxValue((base, ()), [x])

    def active(self):
        B = self.B
        mask = self.mask
        if not self.frames:
            return mask
        frame = self.frames[-1]
        exclusions = [frame.done]
        for loop in frame.loops:
            exclusions.append(loop.brk)
            exclusions.append(loop.cont)
        for ex in exclusions:
            known = B.known(ex)
            if known is not None:
                if known == 0.0:
                    continue
                return 0.0
            inv = B.sub(1.0, ex)
            known_mask = B.known(mask)
            if known_mask is not None:
                mask = inv if known_mask != 0.0 else 0.0
            else:
                mask = B.mul(mask, inv)
        return mask

    def inactive(self):
        known = self.B.known(self.active())
        return known is not None and known == 0.0

    def lookup(self, name):
        if self.frames:
            for scope in reversed(self.frames[-1].scopes):
                if name in scope:
                    return scope, name
        return None, name

    def global_value(self, name):
        if name in self.overrides:
            return self.overrides[name]
        if name in self.cache:
            return self.cache[name]
        g = self.program.globals.get(name)
        if g is None:
            raise FxError("unknown identifier %s" % name)
        if name in self.evaluating:
            raise FxError("recursive global %s" % name)
        self.evaluating.add(name)
        saved_frames, saved_mask = self.frames, self.mask
        self.frames, self.mask = [], 1.0
        try:
            value = self.env.global_value(self, g)
            if value is None:
                value = self.default_global(g)
        finally:
            self.frames, self.mask = saved_frames, saved_mask
            self.evaluating.discard(name)
        self.cache[name] = value
        if self.B.wants_text:
            self.tag(value, name)
        return value

    def default_global(self, g):
        t = fx_resolve_type(g.type, self.program)
        if t == "sampler":
            texture = None
            states = {}
            if g.init is not None and g.init[0] == "sampler_state":
                states = g.init[1]
                tex = states.get("texture")
                if tex:
                    texture = "".join(tex).strip("<>() ")
            return FxValue("sampler", FxSampler(g.name, texture, states, g.register))
        if t == "texture":
            return FxValue("texture", g.name)
        if t == "string":
            return FxValue("string", self.eval(g.init).c if g.init is not None else "")
        if g.array is not None and not fx_is_array(t):
            size = self.const_int(g.array) if g.array is not None else None
            at = fx_array_type(t, size)
            if g.init is not None:
                return self.convert(self.eval_init(g.init, at), at)
            return self.zero(at)
        if g.init is None:
            return self.zero(t)
        if g.init[0] == "init":
            return self.eval_init(g.init, t)
        if g.init[0] == "compile":
            return FxValue("void", None)
        return self.convert(self.eval(g.init), t)

    def eval_init(self, node, t):
        if fx_is_numeric(t):
            comps = []
            for item in self.flatten_init(node):
                comps.extend(item.c if isinstance(item.c, list) else [item.c])
            n = fx_count(t[1])
            if len(comps) == 1 and n > 1:
                comps = comps * n
            comps = (comps + [self.B.const(0.0)] * n)[:n]
            return self.convert(FxValue(("f", t[1]), comps), t)
        if fx_is_array(t):
            items = node[1] if node[0] == "init" else [node]
            elems = []
            flat = None
            if (
                fx_is_numeric(t[1])
                and items
                and not (isinstance(items[0], tuple) and items[0][0] == "init")
            ):
                flat = []
                for item in items:
                    flat.extend(self.eval(item).c)
                per = fx_count(t[1][1])
                if len(flat) >= per * t[2] and len(items) != t[2]:
                    for k in range(t[2]):
                        elems.append(
                            self.convert(
                                FxValue(("f", t[1][1]), flat[k * per : (k + 1) * per]),
                                t[1],
                            )
                        )
                    return FxValue(t, elems)
            for k in range(t[2]):
                if k < len(items):
                    item = items[k]
                    if isinstance(item, tuple) and item[0] == "init":
                        elems.append(self.eval_init(item, t[1]))
                    else:
                        elems.append(self.convert(self.eval(item), t[1]))
                else:
                    elems.append(self.zero(t[1]))
            return FxValue(t, elems)
        if isinstance(t, FxStruct):
            items = node[1] if node[0] == "init" else [node]
            value = self.zero(t)
            flat = []
            for item in items:
                if isinstance(item, tuple) and item[0] == "init":
                    flat.append(item)
                else:
                    flat.append(self.eval(item))
            k = 0
            for name, type_name, _sem, _array in t.members:
                mt = value.c[name].t
                if k >= len(flat):
                    break
                item = flat[k]
                if isinstance(item, tuple):
                    value.c[name] = self.eval_init(item, mt)
                    k += 1
                elif (
                    fx_is_numeric(mt)
                    and fx_is_numeric(item.t)
                    and fx_count(item.t[1]) < fx_count(mt[1])
                ):
                    comps = []
                    while k < len(flat) and len(comps) < fx_count(mt[1]):
                        comps.extend(flat[k].c)
                        k += 1
                    value.c[name] = self.convert(
                        FxValue(("f", mt[1]), comps[: fx_count(mt[1])]), mt
                    )
                else:
                    value.c[name] = self.convert(item, mt)
                    k += 1
            return value
        return self.eval(node)

    def flatten_init(self, node):
        if isinstance(node, tuple) and node[0] == "init":
            out = []
            for item in node[1]:
                out.extend(self.flatten_init(item))
            return out
        return [self.eval(node)]

    def convert(self, value, t):
        B = self.B
        if t is None or t == "void":
            return value
        if fx_is_numeric(t):
            if not fx_is_numeric(value.t):
                if isinstance(value.t, FxStruct):
                    raise FxError("cannot convert struct to %s" % (t,))
                raise FxError("cannot convert %s to %s" % (value.t, t))
            base, dims = t
            sbase, sdims = value.t
            comps = value.c
            n = fx_count(dims)
            if len(dims) == 2 and len(sdims) == 2 and sdims != dims:
                r, c = dims
                sr, sc = sdims
                out = []
                for i in range(r):
                    for j in range(c):
                        out.append(
                            comps[i * sc + j] if i < sr and j < sc else B.const(0.0)
                        )
                comps = out
            elif len(comps) == 1 and n > 1:
                comps = comps * n
            elif len(comps) >= n:
                comps = comps[:n]
            else:
                comps = comps + [B.const(0.0)] * (n - len(comps))
            if base != sbase:
                if base == "i":
                    comps = [B.unary("trunc", x) for x in comps]
                elif base == "b":
                    comps = [B.truth(x) for x in comps]
            return FxValue(t, list(comps))
        if isinstance(t, FxStruct):
            if isinstance(value.t, FxStruct):
                return FxValue(t, dict(value.c))
            if fx_is_numeric(value.t):
                out = {}
                for name, type_name, _sem, array in t.members:
                    mt = fx_resolve_type(type_name, self.program)
                    if array is not None:
                        size = int(self.const_int(array))
                        out[name] = FxValue(
                            fx_array_type(mt, size),
                            [self.convert(value, mt) for _ in range(size)],
                        )
                    elif fx_is_numeric(mt):
                        out[name] = self.convert(FxValue(value.t, [value.c[0]]), mt)
                    else:
                        out[name] = self.convert(value, mt)
                return FxValue(t, out)
        if fx_is_array(t):
            if fx_is_array(value.t):
                return FxValue(t, [self.convert(v, t[1]) for v in value.c][: t[2]])
        return value

    def run_function(self, function, args):
        if self.depth > FX_CALL_DEPTH:
            raise FxUnsupported("call depth exceeded")
        frame = FxFrame(function)
        scope = frame.scopes[0]
        for param, arg in zip(function.params, args):
            pt = fx_resolve_type(param.type, self.program)
            if param.array is not None and not fx_is_array(pt):
                pt = fx_array_type(pt, self.const_int(param.array))
            if arg is None:
                scope[param.name] = (
                    self.zero(pt)
                    if param.default is None
                    else self.convert(self.eval(param.default), pt)
                )
            else:
                scope[param.name] = self.convert(arg, pt)
        saved_mask = self.mask
        self.mask = 1.0
        self.frames.append(frame)
        self.depth += 1
        self.B.enter(function.name)
        try:
            self.exec_block(function.body[1])
        finally:
            self.B.leave()
            self.depth -= 1
            self.frames.pop()
            self.mask = saved_mask
        rt = fx_resolve_type(function.ret, self.program)
        result = (
            frame.ret
            if frame.ret is not None
            else (self.zero(rt) if rt != "void" else FxValue("void", None))
        )
        outs = {}
        for param in function.params:
            if "out" in param.quals or "inout" in param.quals:
                outs[param.name] = scope[param.name]
        return result, outs

    def exec_block(self, statements):
        for statement in statements:
            if self.inactive():
                return
            self.exec(statement)

    def exec(self, node):
        kind = node[0]
        if self.B.wants_text and kind in FX_TEXT_STATEMENTS:
            self.B.statement(fx_stmt_text(node))
        if kind == "expr":
            self.eval(node[1])
        elif kind == "decl":
            self.exec_decl(node)
        elif kind == "block":
            frame = self.frames[-1]
            frame.scopes.append({})
            try:
                self.exec_block(node[1])
            finally:
                frame.scopes.pop()
        elif kind == "if":
            self.exec_if(node)
        elif kind == "for":
            self.exec_for(node)
        elif kind == "do":
            self.exec_do(node)
        elif kind == "return":
            self.exec_return(node)
        elif kind == "break":
            loop = self.frames[-1].loops[-1]
            loop.brk = self.or_mask(loop.brk, self.active())
        elif kind == "continue":
            loop = self.frames[-1].loops[-1]
            loop.cont = self.or_mask(loop.cont, self.active())
        elif kind == "discard":
            self.discard = self.or_mask(self.discard, self.active())
        elif kind == "empty":
            pass
        else:
            raise FxError("unknown statement %s" % kind)

    def or_mask(self, a, b):
        B = self.B
        ka, kb = B.known(a), B.known(b)
        if ka is not None and kb is not None:
            return 1.0 if (ka or kb) else 0.0
        if ka is not None:
            return 1.0 if ka else b
        if kb is not None:
            return 1.0 if kb else a
        return B.max(a, b)

    def exec_decl(self, node):
        _kind, type_name, declarators, _quals = node
        t = fx_resolve_type(type_name, self.program)
        scope = self.frames[-1].scopes[-1]
        for name, array, init in declarators:
            vt = t
            if array is not None:
                vt = fx_array_type(t, self.const_int(array))
            if init is None:
                scope[name] = self.zero(vt)
            elif isinstance(init, tuple) and init[0] == "init":
                scope[name] = self.eval_init(init, vt)
            else:
                scope[name] = self.convert(self.eval(init), vt)
            if init is not None:
                self.tag(scope[name], name)

    def tag(self, value, label):
        if fx_is_numeric(value.t):
            comps = value.c
            dims = value.t[1]
            for k, comp in enumerate(comps):
                if len(comps) == 1:
                    suffix = ""
                elif len(dims) == 2:
                    suffix = "[%d][%d]" % divmod(k, dims[1])
                else:
                    suffix = "." + "xyzw"[k] if k < 4 else "[%d]" % k
                self.B.tag(comp, label + suffix)
        elif isinstance(value.t, FxStruct):
            for name, member in value.c.items():
                self.tag(member, "%s.%s" % (label, name))

    def condition(self, expr):
        value = self.eval(expr)
        if not fx_is_numeric(value.t):
            raise FxError("condition is not numeric")
        return self.B.truth(value.c[0])

    def exec_if(self, node):
        _kind, cond_expr, then, other = node
        c = self.condition(cond_expr)
        known = self.B.known(c)
        if known is not None:
            if known:
                self.exec(then)
            elif other is not None:
                self.exec(other)
            return
        B = self.B
        saved = self.mask
        base = self.active()
        known_base = B.known(base)
        self.mask = c if known_base is not None else B.mul(base, c)
        try:
            self.exec(then)
            if other is not None:
                inv = B.sub(1.0, c)
                self.mask = inv if known_base is not None else B.mul(base, inv)
                self.exec(other)
        finally:
            self.mask = saved

    def exec_for(self, node):
        _kind, init, cond, step, body = node
        frame = self.frames[-1]
        frame.scopes.append({})
        loop = FxLoop()
        try:
            self.exec(init)
            frame.loops.append(loop)
            for _ in range(FX_LOOP_LIMIT):
                if self.inactive():
                    break
                if cond is not None:
                    c = self.condition(cond)
                    known = self.B.known(c)
                    if known is not None:
                        if not known:
                            break
                    else:
                        if not self.B.vectorized:
                            raise FxUnsupported("data-dependent loop")
                        loop.brk = self.or_mask(loop.brk, self.B.sub(1.0, c))
                        if not self.B.any_true(self.active()):
                            break
                self.exec(body)
                loop.cont = 0.0
                if step is not None and not self.inactive():
                    self.eval(step)
            else:
                raise FxUnsupported("loop did not finish")
        finally:
            if frame.loops and frame.loops[-1] is loop:
                frame.loops.pop()
            frame.scopes.pop()

    def exec_do(self, node):
        _kind, body, cond = node
        frame = self.frames[-1]
        loop = FxLoop()
        frame.loops.append(loop)
        try:
            for _ in range(FX_LOOP_LIMIT):
                if self.inactive():
                    break
                self.exec(body)
                loop.cont = 0.0
                if self.inactive():
                    break
                c = self.condition(cond)
                known = self.B.known(c)
                if known is not None:
                    if not known:
                        break
                else:
                    if not self.B.vectorized:
                        raise FxUnsupported("data-dependent loop")
                    loop.brk = self.or_mask(loop.brk, self.B.sub(1.0, c))
                    if not self.B.any_true(self.active()):
                        break
        finally:
            frame.loops.pop()

    def exec_return(self, node):
        frame = self.frames[-1]
        rt = fx_resolve_type(frame.function.ret, self.program)
        mask = self.active()
        if node[1] is not None and rt != "void":
            value = self.convert(self.eval(node[1]), rt)
            known = self.B.known(mask)
            if frame.ret is None or (known is not None and known):
                frame.ret = value
            else:
                frame.ret = self.merge(mask, value, frame.ret)
        frame.done = self.or_mask(frame.done, mask)

    def merge(self, mask, new, old):
        B = self.B
        if fx_is_numeric(new.t):
            return FxValue(new.t, [B.select(mask, a, b) for a, b in zip(new.c, old.c)])
        if isinstance(new.t, FxStruct):
            return FxValue(
                new.t, {k: self.merge(mask, new.c[k], old.c[k]) for k in new.c}
            )
        if fx_is_array(new.t):
            return FxValue(
                new.t, [self.merge(mask, a, b) for a, b in zip(new.c, old.c)]
            )
        return new

    def eval(self, node):
        kind = node[0]
        method = FX_EVAL.get(kind)
        if method is None:
            raise FxError("cannot evaluate %s" % kind)
        return method(self, node)

    def eval_num(self, node):
        return FxValue((node[2], ()), [self.B.const(node[1])])

    def eval_str(self, node):
        return FxValue("string", node[1])

    def eval_id(self, node):
        scope, name = self.lookup(node[1])
        if scope is not None:
            return scope[name]
        return self.global_value(name)

    def eval_comma(self, node):
        result = None
        for item in node[1]:
            result = self.eval(item)
        return result

    def eval_init_expr(self, node):
        items = self.flatten_init(node)
        comps = []
        for item in items:
            comps.extend(item.c)
        dims = () if len(comps) == 1 else (len(comps),)
        if len(comps) > 4:
            dims = (len(comps),)
        return FxValue(("f", dims), comps)

    def eval_un(self, node):
        op = node[1]
        value = self.eval(node[2])
        B = self.B
        if op == "+":
            return value
        if op == "-":
            return FxValue(value.t, [B.neg(x) for x in value.c])
        if op == "!":
            return FxValue(("b", value.t[1]), [B.lnot(x) for x in value.c])
        if op == "~":
            return FxValue(value.t, [B.bitnot(x) for x in value.c])
        raise FxError("unary %s" % op)

    def eval_incdec(self, node):
        kind, op, target = node
        old = self.eval(target)
        one = FxValue(("i", ()), [self.B.const(1.0)])
        new = self.binary("+" if op == "++" else "-", old, one)
        self.assign(target, self.convert(new, old.t))
        return new if kind == "pre" else old

    def eval_bin(self, node):
        return self.binary(node[1], self.eval(node[2]), self.eval(node[3]))

    def broadcast(self, a, b):
        ta, tb = a.t, b.t
        if not (fx_is_numeric(ta) and fx_is_numeric(tb)):
            raise FxError("non-numeric operand")
        da, db = ta[1], tb[1]
        na, nb = len(a.c), len(b.c)
        if da == db:
            return da, a.c, b.c
        if na == 1:
            return db, a.c * nb, b.c
        if nb == 1:
            return da, a.c, b.c * na
        if len(da) == 1 and len(db) == 1:
            k = min(da[0], db[0])
            return (k,), a.c[:k], b.c[:k]
        if len(da) == 2 and len(db) == 2:
            r, c = min(da[0], db[0]), min(da[1], db[1])
            ca = [a.c[i * da[1] + j] for i in range(r) for j in range(c)]
            cb = [b.c[i * db[1] + j] for i in range(r) for j in range(c)]
            return (r, c), ca, cb
        if na == nb:
            return da, a.c, b.c
        k = min(na, nb)
        return (k,), a.c[:k], b.c[:k]

    def binary(self, op, a, b):
        B = self.B
        dims, ca, cb = self.broadcast(a, b)
        fa, fb = a.t[0], b.t[0]
        base = "f" if "f" in (fa, fb) else "i"
        if op in ("+", "-", "*", "/", "%"):
            if op == "+":
                comps = [B.add(x, y) for x, y in zip(ca, cb)]
            elif op == "-":
                comps = [B.sub(x, y) for x, y in zip(ca, cb)]
            elif op == "*":
                comps = [B.mul(x, y) for x, y in zip(ca, cb)]
            elif op == "/":
                if base == "i":
                    comps = [B.idiv(x, y) for x, y in zip(ca, cb)]
                else:
                    comps = [B.div(x, y) for x, y in zip(ca, cb)]
            elif base == "i":
                comps = [B.imod(x, y) for x, y in zip(ca, cb)]
            else:
                comps = [B.mod(x, y) for x, y in zip(ca, cb)]
            return FxValue((base, dims), comps)
        if op in ("<", "<=", ">", ">=", "==", "!="):
            return FxValue(("b", dims), [B.cmp(op, x, y) for x, y in zip(ca, cb)])
        if op == "&&":
            return FxValue(("b", dims), [B.land(x, y) for x, y in zip(ca, cb)])
        if op == "||":
            return FxValue(("b", dims), [B.lor(x, y) for x, y in zip(ca, cb)])
        if op in ("&", "|", "^", "<<", ">>"):
            result_base = (
                "b" if fa == "b" and fb == "b" and op in ("&", "|", "^") else "i"
            )
            return FxValue(
                (result_base, dims), [B.bitop(op, x, y) for x, y in zip(ca, cb)]
            )
        raise FxError("binary %s" % op)

    def eval_assign(self, node):
        _kind, op, target, value_expr = node
        value = self.eval(value_expr)
        if op != "=":
            current = self.eval(target)
            value = self.binary(op[:-1], current, value)
        self.assign(target, value)
        result = self.eval(target)
        self.tag(result, fx_target_label(target))
        return result

    def eval_tern(self, node):
        c = self.condition(node[1])
        known = self.B.known(c)
        if known is not None:
            return self.eval(node[2] if known else node[3])
        a = self.eval(node[2])
        b = self.eval(node[3])
        if fx_is_numeric(a.t) and fx_is_numeric(b.t):
            dims, ca, cb = self.broadcast(a, b)
            base = "f" if "f" in (a.t[0], b.t[0]) else a.t[0]
            return FxValue(
                (base, dims), [self.B.select(c, x, y) for x, y in zip(ca, cb)]
            )
        return self.merge(c, a, self.convert(b, a.t))

    def eval_cast(self, node):
        t = fx_resolve_type(node[1], self.program)
        value = self.eval(node[2])
        return self.convert(value, t)

    def eval_ctor(self, node):
        t = fx_resolve_type(node[1], self.program)
        args = [self.eval(a) for a in node[2]]
        if isinstance(t, FxStruct):
            if len(args) == 1:
                return self.convert(args[0], t)
            raise FxUnsupported("struct constructor")
        comps = []
        for arg in args:
            comps.extend(arg.c)
        n = fx_count(t[1])
        if len(comps) == 1 and n > 1:
            comps = comps * n
        comps = (comps + [self.B.const(0.0)] * n)[:n]
        source_base = "f" if any(a.t[0] == "f" for a in args) else "i"
        return self.convert(FxValue((source_base, t[1]), comps), t)

    def swizzle_indices(self, t, name):
        if not fx_is_numeric(t):
            return None
        dims = t[1]
        if len(dims) == 2:
            found = re.findall(r"_m(\d)(\d)|_(\d)(\d)", name)
            if (
                not found
                or "".join(
                    ("_m%s%s" % (a, b)) if a else ("_%s%s" % (c, d))
                    for a, b, c, d in found
                )
                != name
            ):
                return None
            out = []
            for a, b, c, d in found:
                if a:
                    out.append(int(a) * dims[1] + int(b))
                else:
                    out.append((int(c) - 1) * dims[1] + (int(d) - 1))
            return out
        if len(name) > 4 or any(ch not in FX_SWIZZLE for ch in name):
            return None
        return [FX_SWIZZLE[ch] for ch in name]

    def eval_member(self, node):
        value = self.eval(node[1])
        name = node[2]
        if isinstance(value.t, FxStruct):
            if name not in value.c:
                raise FxError("struct %s has no member %s" % (value.t.name, name))
            return value.c[name]
        indices = self.swizzle_indices(value.t, name)
        if indices is None:
            raise FxError("bad member %s" % name)
        comps = value.c
        out = []
        for i in indices:
            out.append(
                comps[i]
                if i < len(comps)
                else comps[0] if len(comps) == 1 else self.B.const(0.0)
            )
        dims = () if len(out) == 1 else (len(out),)
        return FxValue((value.t[0], dims), out)

    def eval_index(self, node):
        value = self.eval(node[1])
        index = self.eval(node[2])
        known = self.B.known(index.c[0])
        if fx_is_array(value.t):
            if known is not None:
                k = int(known)
                return value.c[max(0, min(k, len(value.c) - 1))]
            return self.dynamic_pick(value.c, index.c[0])
        if fx_is_numeric(value.t):
            dims = value.t[1]
            if len(dims) == 2:
                rows = [
                    FxValue(
                        (value.t[0], (dims[1],)),
                        value.c[r * dims[1] : (r + 1) * dims[1]],
                    )
                    for r in range(dims[0])
                ]
                if known is not None:
                    return rows[max(0, min(int(known), dims[0] - 1))]
                return self.dynamic_pick(rows, index.c[0])
            elems = [FxValue((value.t[0], ()), [x]) for x in value.c]
            if known is not None:
                return elems[max(0, min(int(known), len(elems) - 1))]
            return self.dynamic_pick(elems, index.c[0])
        raise FxError("cannot index")

    def dynamic_pick(self, items, index):
        B = self.B
        result = items[0]
        for k in range(1, len(items)):
            hit = B.cmp("==", B.unary("trunc", index), B.const(float(k)))
            result = self.merge(hit, items[k], result)
        return result

    def lvalue_path(self, node):
        path = []
        while True:
            kind = node[0]
            if kind == "id":
                return node[1], list(reversed(path))
            if kind == "member":
                path.append(("m", node[2]))
                node = node[1]
            elif kind == "index":
                path.append(("i", node[2]))
                node = node[1]
            else:
                raise FxError("expression is not assignable")

    def assign(self, target, value):
        name, path = self.lvalue_path(target)
        scope, _ = self.lookup(name)
        if scope is None:
            if name in self.overrides or name in self.program.globals:
                current = self.global_value(name)
                updated = self.write(current, path, value, self.active())
                self.cache[name] = updated
                return
            raise FxError("unknown variable %s" % name)
        scope[name] = self.write(scope[name], path, value, self.active())

    def write(self, current, path, value, mask):
        B = self.B
        if not path:
            new = self.convert(value, current.t) if current.t is not None else value
            known = B.known(mask)
            if known is not None:
                return new if known else current
            return self.merge(mask, new, current)
        kind, arg = path[0]
        if kind == "m":
            if isinstance(current.t, FxStruct):
                members = dict(current.c)
                members[arg] = self.write(members[arg], path[1:], value, mask)
                return FxValue(current.t, members)
            indices = self.swizzle_indices(current.t, arg)
            if indices is None:
                raise FxError("bad member %s" % arg)
            sub = FxValue(
                (current.t[0], () if len(indices) == 1 else (len(indices),)),
                [current.c[i] for i in indices],
            )
            sub = self.write(sub, path[1:], value, mask)
            comps = list(current.c)
            for j, i in enumerate(indices):
                comps[i] = sub.c[j]
            return FxValue(current.t, comps)
        index = self.eval(arg)
        known = B.known(index.c[0])
        if fx_is_array(current.t):
            elems = list(current.c)
            if known is not None:
                k = max(0, min(int(known), len(elems) - 1))
                elems[k] = self.write(elems[k], path[1:], value, mask)
            else:
                for k in range(len(elems)):
                    hit = B.cmp("==", B.unary("trunc", index.c[0]), B.const(float(k)))
                    sub_mask = hit if B.known(mask) is not None else B.mul(mask, hit)
                    elems[k] = self.write(elems[k], path[1:], value, sub_mask)
            return FxValue(current.t, elems)
        dims = current.t[1]
        comps = list(current.c)
        if len(dims) == 2:
            width = dims[1]
            groups = [list(range(r * width, (r + 1) * width)) for r in range(dims[0])]
            sub_type = (current.t[0], (width,))
        else:
            groups = [[i] for i in range(len(comps))]
            sub_type = (current.t[0], ())
        targets = (
            range(len(groups))
            if known is None
            else [max(0, min(int(known), len(groups) - 1))]
        )
        for k in targets:
            sub_mask = mask
            if known is None:
                hit = B.cmp("==", B.unary("trunc", index.c[0]), B.const(float(k)))
                sub_mask = hit if B.known(mask) is not None else B.mul(mask, hit)
            sub = FxValue(sub_type, [comps[i] for i in groups[k]])
            sub = self.write(sub, path[1:], value, sub_mask)
            for j, i in enumerate(groups[k]):
                comps[i] = sub.c[j]
        return FxValue(current.t, comps)

    def find_function(self, name, args):
        candidates = self.program.functions.get(name)
        if not candidates:
            return None
        fitting = [
            f
            for f in candidates
            if len(f.params) >= len(args)
            and all(p.default is not None for p in f.params[len(args) :])
        ]
        if not fitting:
            return None
        if len(fitting) == 1:
            return fitting[0]
        best = None
        best_score = -1
        for f in fitting:
            score = 0
            for p, a in zip(f.params, args):
                try:
                    pt = fx_resolve_type(p.type, self.program)
                except FxError:
                    continue
                if fx_is_numeric(pt) and fx_is_numeric(a.t) and pt[1] == a.t[1]:
                    score += 2
                elif pt == a.t:
                    score += 2
            if score > best_score:
                best, best_score = f, score
        return best

    def eval_call(self, node):
        name = node[1]
        arg_nodes = node[2]
        if name in self.program.functions:
            args = [self.eval(a) for a in arg_nodes]
            function = self.find_function(name, args)
            if function is not None:
                if self.B.wants_text:
                    grouped = self.B.call_group(self, function, args)
                    if grouped is not None:
                        return grouped
                result, outs = self.run_function(function, args)
                for param, arg_node in zip(function.params, arg_nodes):
                    if param.name in outs:
                        self.assign(arg_node, outs[param.name])
                return result
        intrinsic = FX_INTRINSICS.get(name)
        if intrinsic is None:
            raise FxUnsupported("unknown function %s" % name)
        if self.B.wants_text and (
            name in FX_GROUP_INTRINSICS or name in FX_NATIVE_INTRINSICS
        ):
            args = [self.eval(a) for a in arg_nodes]
            native = (
                self.B.native_intrinsic(self, name, args)
                if name in FX_NATIVE_INTRINSICS
                else None
            )
            if native is not None:
                return native
            grouped = (
                self.B.intrinsic_group(self, name, args)
                if name in FX_GROUP_INTRINSICS
                else None
            )
            if grouped is not None:
                return grouped
            return fx_intrinsic_values(self, name, args)
        return intrinsic(self, arg_nodes)

    def eval_mcall(self, node):
        _kind, obj, name, args = node
        lower = name.lower()
        if lower in ("sample", "samplelevel", "samplebias", "samplegrad"):
            texture = self.eval(obj)
            sampler = self.eval(args[0])
            uv = self.eval(args[1])
            info = sampler.c
            if isinstance(info, FxSampler) and texture.t == "texture":
                info = FxSampler(info.name, texture.c, info.states, info.register)
            lod = None
            if lower == "samplelevel" and len(args) > 2:
                lod = self.eval(args[2]).c[0]
            return self.sample(
                info, uv.c[0], uv.c[1] if len(uv.c) > 1 else self.B.const(0.0), lod
            )
        raise FxUnsupported("method %s" % name)

    def sample(self, info, u, v, lod=None):
        comps = self.env.sample(self, info, u, v, lod)
        return FxValue(("f", (4,)), comps)

    def elementwise(self, fn, *values):
        if len(values) == 1:
            value = values[0]
            return FxValue(
                ("f" if value.t[0] != "i" else "i", value.t[1]),
                [fn(x) for x in value.c],
            )
        dims = values[0].t[1]
        comps = [values[0].c]
        result_dims = dims
        current = values[0]
        for other in values[1:]:
            result_dims, ca, cb = self.broadcast(current, other)
            current = FxValue(("f", result_dims), ca)
            comps = [
                (
                    c
                    if len(c) == len(ca)
                    else (c * len(ca) if len(c) == 1 else c[: len(ca)])
                )
                for c in comps
            ]
            comps.append(cb)
        size = max(len(c) for c in comps)
        comps = [c * size if len(c) == 1 else c[:size] for c in comps]
        base = "f" if any(v.t[0] == "f" for v in values) else "i"
        return FxValue((base, result_dims), [fn(*parts) for parts in zip(*comps)])


def fx_target_label(node):
    kind = node[0]
    if kind == "id":
        return node[1]
    if kind == "member":
        return "%s.%s" % (fx_target_label(node[1]), node[2])
    if kind == "index":
        inner = node[2]
        index = inner[1] if inner[0] in ("num", "id") else "i"
        if isinstance(index, float) and index.is_integer():
            index = int(index)
        return "%s[%s]" % (fx_target_label(node[1]), index)
    return "value"


FX_EVAL = {
    "num": FxExec.eval_num,
    "str": FxExec.eval_str,
    "id": FxExec.eval_id,
    "bin": FxExec.eval_bin,
    "un": FxExec.eval_un,
    "pre": FxExec.eval_incdec,
    "post": FxExec.eval_incdec,
    "assign": FxExec.eval_assign,
    "tern": FxExec.eval_tern,
    "cast": FxExec.eval_cast,
    "ctor": FxExec.eval_ctor,
    "member": FxExec.eval_member,
    "index": FxExec.eval_index,
    "call": FxExec.eval_call,
    "mcall": FxExec.eval_mcall,
    "comma": FxExec.eval_comma,
    "init": FxExec.eval_init_expr,
}


def fx_args(ex, nodes):
    return [ex.eval(n) for n in nodes]


def fx_unary_intrinsic(name):
    def run(ex, nodes):
        (a,) = fx_args(ex, nodes)
        return ex.elementwise(
            lambda x: ex.B.unary(name, x), FxValue(("f", a.t[1]), a.c)
        )

    return run


def fx_saturate_scalar(B, x):
    return B.min(B.max(x, 0.0), 1.0)


def fx_i_saturate(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), [fx_saturate_scalar(ex.B, x) for x in a.c])


def fx_i_clamp(ex, nodes):
    a, lo, hi = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(lambda x, l, h: B.min(B.max(x, l), h), a, lo, hi)


def fx_i_min(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.min, a, b)


def fx_i_max(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.max, a, b)


def fx_i_pow(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.pow, FxValue(("f", a.t[1]), a.c), b)


def fx_i_fmod(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.mod, FxValue(("f", a.t[1]), a.c), b)


def fx_i_atan2(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.atan2, FxValue(("f", a.t[1]), a.c), b)


def fx_i_lerp(ex, nodes):
    a, b, t = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(
        lambda x, y, s: B.add(x, B.mul(B.sub(y, x), s)),
        FxValue(("f", a.t[1]), a.c),
        b,
        t,
    )


def fx_i_step(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(
        lambda edge, x: B.cmp(">=", x, edge), FxValue(("f", a.t[1]), a.c), b
    )


def fx_i_smoothstep(ex, nodes):
    lo, hi, x = fx_args(ex, nodes)
    B = ex.B

    def run(a, b, v):
        t = fx_saturate_scalar(B, B.div(B.sub(v, a), B.sub(b, a)))
        return B.mul(B.mul(t, t), B.sub(3.0, B.mul(2.0, t)))

    return ex.elementwise(run, FxValue(("f", lo.t[1]), lo.c), hi, x)


def fx_dot_comps(B, a, b):
    total = None
    for x, y in zip(a, b):
        term = B.mul(x, y)
        total = term if total is None else B.add(total, term)
    return total if total is not None else B.const(0.0)


def fx_i_dot(ex, nodes):
    a, b = fx_args(ex, nodes)
    _dims, ca, cb = ex.broadcast(a, b)
    return FxValue(("f", ()), [fx_dot_comps(ex.B, ca, cb)])


def fx_i_cross(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    ax, ay, az = (a.c + [B.const(0.0)] * 3)[:3]
    bx, by, bz = (b.c + [B.const(0.0)] * 3)[:3]
    return FxValue(
        ("f", (3,)),
        [
            B.sub(B.mul(ay, bz), B.mul(az, by)),
            B.sub(B.mul(az, bx), B.mul(ax, bz)),
            B.sub(B.mul(ax, by), B.mul(ay, bx)),
        ],
    )


def fx_length_comps(B, comps):
    return B.unary("sqrt", fx_dot_comps(B, comps, comps))


def fx_i_length(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", ()), [fx_length_comps(ex.B, a.c)])


def fx_i_distance(ex, nodes):
    a, b = fx_args(ex, nodes)
    diff = ex.binary("-", a, b)
    return FxValue(("f", ()), [fx_length_comps(ex.B, diff.c)])


def fx_safe_normalize(B, comps):
    length = fx_length_comps(B, comps)
    inv = B.select(B.cmp(">", length, 1e-20), B.div(1.0, length), 0.0)
    return [B.mul(x, inv) for x in comps]


def fx_i_normalize(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), fx_safe_normalize(ex.B, a.c))


def fx_i_reflect(ex, nodes):
    i, n = fx_args(ex, nodes)
    B = ex.B
    d = B.mul(2.0, fx_dot_comps(B, i.c, n.c))
    return FxValue(("f", i.t[1]), [B.sub(x, B.mul(d, y)) for x, y in zip(i.c, n.c)])


def fx_i_refract(ex, nodes):
    i, n, eta = fx_args(ex, nodes)
    B = ex.B
    e = eta.c[0]
    cosi = fx_dot_comps(B, i.c, n.c)
    k = B.sub(1.0, B.mul(B.mul(e, e), B.sub(1.0, B.mul(cosi, cosi))))
    root = B.unary("sqrt", B.max(k, 0.0))
    factor = B.add(B.mul(e, cosi), root)
    comps = [B.sub(B.mul(e, x), B.mul(factor, y)) for x, y in zip(i.c, n.c)]
    ok = B.cmp(">=", k, 0.0)
    return FxValue(("f", i.t[1]), [B.select(ok, x, 0.0) for x in comps])


def fx_i_faceforward(ex, nodes):
    n, i, ng = fx_args(ex, nodes)
    B = ex.B
    d = fx_dot_comps(B, i.c, ng.c)
    flip = B.cmp("<", d, 0.0)
    return FxValue(("f", n.t[1]), [B.select(flip, x, B.neg(x)) for x in n.c])


def fx_i_mul(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    da, db = a.t[1], b.t[1]
    if len(da) == 0 or len(db) == 0:
        return ex.binary("*", a, b)
    if len(da) == 1 and len(db) == 1:
        return FxValue(("f", ()), [fx_dot_comps(B, a.c, b.c)])
    if len(da) == 1 and len(db) == 2:
        rows, cols = db
        k = min(da[0], rows)
        out = []
        for j in range(cols):
            out.append(fx_dot_comps(B, a.c[:k], [b.c[i * cols + j] for i in range(k)]))
        return FxValue(("f", (cols,)), out)
    if len(da) == 2 and len(db) == 1:
        rows, cols = da
        k = min(cols, db[0])
        out = []
        for i in range(rows):
            out.append(fx_dot_comps(B, a.c[i * cols : i * cols + k], b.c[:k]))
        return FxValue(("f", (rows,)), out)
    r1, c1 = da
    r2, c2 = db
    k = min(c1, r2)
    out = []
    for i in range(r1):
        for j in range(c2):
            out.append(
                fx_dot_comps(
                    B,
                    [a.c[i * c1 + m] for m in range(k)],
                    [b.c[m * c2 + j] for m in range(k)],
                )
            )
    return FxValue(("f", (r1, c2)), out)


def fx_i_transpose(ex, nodes):
    (a,) = fx_args(ex, nodes)
    if len(a.t[1]) != 2:
        return a
    r, c = a.t[1]
    return FxValue(
        (a.t[0], (c, r)), [a.c[i * c + j] for j in range(c) for i in range(r)]
    )


def fx_i_determinant(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    n = a.t[1][0]

    def det(rows, cols):
        if len(rows) == 1:
            return a.c[rows[0] * n + cols[0]]
        total = None
        for k, col in enumerate(cols):
            sub = det(rows[1:], cols[:k] + cols[k + 1 :])
            term = B.mul(a.c[rows[0] * n + col], sub)
            if k % 2:
                term = B.neg(term)
            total = term if total is None else B.add(total, term)
        return total

    return FxValue(("f", ()), [det(list(range(n)), list(range(n)))])


def fx_i_all(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    total = B.const(1.0)
    for x in a.c:
        total = B.mul(total, B.truth(x))
    return FxValue(("b", ()), [total])


def fx_i_any(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    total = B.const(0.0)
    for x in a.c:
        total = B.max(total, B.truth(x))
    return FxValue(("b", ()), [total])


def fx_i_clip(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    hit = B.const(0.0)
    for x in a.c:
        hit = B.max(hit, B.cmp("<", x, 0.0))
    active = ex.active()
    known = B.known(active)
    if known is None:
        hit = B.mul(hit, active)
    elif not known:
        return FxValue("void", None)
    ex.discard = ex.or_mask(ex.discard, hit)
    return FxValue("void", None)


def fx_i_zero_like(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), [ex.B.const(0.0)] * len(a.c))


def fx_i_radians(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return ex.elementwise(
        lambda x: ex.B.mul(x, math.pi / 180.0), FxValue(("f", a.t[1]), a.c)
    )


def fx_i_degrees(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return ex.elementwise(
        lambda x: ex.B.mul(x, 180.0 / math.pi), FxValue(("f", a.t[1]), a.c)
    )


def fx_i_isnan(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("b", a.t[1]), [ex.B.cmp("!=", x, x) for x in a.c])


def fx_i_sincos(ex, nodes):
    a = ex.eval(nodes[0])
    s = ex.elementwise(lambda x: ex.B.unary("sin", x), FxValue(("f", a.t[1]), a.c))
    c = ex.elementwise(lambda x: ex.B.unary("cos", x), FxValue(("f", a.t[1]), a.c))
    ex.assign(nodes[1], s)
    ex.assign(nodes[2], c)
    return FxValue("void", None)


def fx_i_ldexp(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(
        lambda x, e: B.mul(x, B.unary("exp2", e)), FxValue(("f", a.t[1]), a.c), b
    )


def fx_i_lit(ex, nodes):
    ndotl, ndoth, m = fx_args(ex, nodes)
    B = ex.B
    d = ndotl.c[0]
    h = ndoth.c[0]
    spec = B.select(
        B.mul(B.cmp(">", d, 0.0), B.cmp(">", h, 0.0)), B.pow(B.max(h, 0.0), m.c[0]), 0.0
    )
    return FxValue(("f", (4,)), [B.const(1.0), B.max(d, 0.0), spec, B.const(1.0)])


def fx_i_as_is(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return a


def fx_texture_call(mode):
    def run(ex, nodes):
        sampler = ex.eval(nodes[0])
        coord = ex.eval(nodes[1])
        B = ex.B
        info = sampler.c
        comps = coord.c
        u = comps[0]
        v = comps[1] if len(comps) > 1 else B.const(0.0)
        lod = None
        if mode == "lod":
            lod = comps[3] if len(comps) > 3 else B.const(0.0)
        elif mode == "proj" and len(comps) > 3:
            u = B.div(u, comps[3])
            v = B.div(v, comps[3])
        elif mode == "1d":
            v = B.const(0.5)
        return ex.sample(info, u, v, lod)

    return run


FX_INTRINSICS = {
    "saturate": fx_i_saturate,
    "clamp": fx_i_clamp,
    "min": fx_i_min,
    "max": fx_i_max,
    "pow": fx_i_pow,
    "fmod": fx_i_fmod,
    "atan2": fx_i_atan2,
    "lerp": fx_i_lerp,
    "step": fx_i_step,
    "smoothstep": fx_i_smoothstep,
    "dot": fx_i_dot,
    "cross": fx_i_cross,
    "length": fx_i_length,
    "distance": fx_i_distance,
    "normalize": fx_i_normalize,
    "reflect": fx_i_reflect,
    "refract": fx_i_refract,
    "faceforward": fx_i_faceforward,
    "mul": fx_i_mul,
    "transpose": fx_i_transpose,
    "determinant": fx_i_determinant,
    "all": fx_i_all,
    "any": fx_i_any,
    "clip": fx_i_clip,
    "ddx": fx_i_zero_like,
    "ddy": fx_i_zero_like,
    "ddx_coarse": fx_i_zero_like,
    "ddy_coarse": fx_i_zero_like,
    "ddx_fine": fx_i_zero_like,
    "ddy_fine": fx_i_zero_like,
    "fwidth": fx_i_zero_like,
    "radians": fx_i_radians,
    "degrees": fx_i_degrees,
    "isnan": fx_i_isnan,
    "sincos": fx_i_sincos,
    "ldexp": fx_i_ldexp,
    "lit": fx_i_lit,
    "asfloat": fx_i_as_is,
    "asint": fx_i_as_is,
    "asuint": fx_i_as_is,
    "tex2D": fx_texture_call("plain"),
    "tex2Dlod": fx_texture_call("lod"),
    "tex2Dbias": fx_texture_call("plain"),
    "tex2Dgrad": fx_texture_call("plain"),
    "tex2Dproj": fx_texture_call("proj"),
    "tex1D": fx_texture_call("1d"),
    "tex1Dlod": fx_texture_call("lod"),
    "tex3D": fx_texture_call("plain"),
    "tex3Dlod": fx_texture_call("lod"),
    "texCUBE": fx_texture_call("plain"),
    "texCUBElod": fx_texture_call("lod"),
}


for _name in (
    "abs",
    "sign",
    "floor",
    "ceil",
    "trunc",
    "round",
    "frac",
    "sqrt",
    "rsqrt",
    "exp",
    "exp2",
    "log",
    "log2",
    "log10",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "sinh",
    "cosh",
    "tanh",
):
    FX_INTRINSICS[_name] = fx_unary_intrinsic(_name)


def fx_const_value(program, node):
    if node is None:
        return None
    ex = FxExec(program, FxNumpyBackend(), FxConstEnv())
    try:
        value = ex.eval(node)
    except FxError:
        return None
    if value.t == "string":
        return value.c
    if fx_is_numeric(value.t):
        out = [float(x) for x in value.c]
        return out[0] if len(out) == 1 else out
    return None


class FxConstEnv:
    def global_value(self, ex, g):
        return None

    def sample(self, ex, info, u, v, lod):
        return [ex.B.const(0.0)] * 4


def fx_annotation(g, name, program, default=None):
    for key, node in g.ann.items():
        if key.lower() == name.lower():
            value = fx_const_value(program, node)
            return default if value is None else value
    return default


def fx_subset_matches(spec, index):
    if spec is None:
        return True
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo = int(lo) if lo else 0
            if hi == "":
                if index >= lo:
                    return True
            elif lo <= index <= int(hi):
                return True
        else:
            try:
                if int(part) == index:
                    return True
            except ValueError:
                pass
    return False


class FxDraw:
    __slots__ = ("technique", "fx_pass", "subset", "loops", "target", "kind", "order")

    def __init__(self, technique, fx_pass, subset, loops, target, kind, order):
        self.technique = technique
        self.fx_pass = fx_pass
        self.subset = subset
        self.loops = loops
        self.target = target
        self.kind = kind
        self.order = order

    @property
    def label(self):
        return "%s.%s" % (self.technique.name or "tech", self.fx_pass.name or "pass")


def fx_technique_pass_type(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "mmdpass":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value.lower()
    return "object"


def fx_technique_subset(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "subset":
            value = fx_const_value(program, node)
            if value is None:
                return None
            if isinstance(value, float):
                return str(int(value))
            return str(value)
    return None


def fx_technique_script(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "script":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value
    return None


def fx_pass_script(fx_pass, program):
    for key, node in fx_pass.ann.items():
        if key.lower() == "script":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value
    return None


def fx_pick_technique(program, subset, pass_types=("object", "object_ss")):
    for pass_type in pass_types:
        for technique in program.techniques:
            if fx_technique_pass_type(technique, program) != pass_type:
                continue
            if fx_subset_matches(fx_technique_subset(technique, program), subset):
                return technique
    return None


def fx_global_int(program, name, values=None):
    name = name.strip()
    try:
        return int(float(name))
    except ValueError:
        pass
    if values and name in values:
        return max(0, int(values[name][0]))
    g = program.globals.get(name)
    if g is None or g.init is None:
        return 1
    value = fx_const_value(program, g.init)
    if isinstance(value, list):
        value = value[0]
    return int(value) if value is not None else 1


def fx_plan_draws(program, subset_count, values=None):
    draws = []
    order = 0
    for subset in range(subset_count):
        technique = fx_pick_technique(program, subset)
        if technique is None:
            continue
        passes = {p.name: p for p in technique.passes}
        script = fx_technique_script(technique, program)
        if script is None:
            for fx_pass in technique.passes:
                kind = (
                    "buffer"
                    if (fx_pass_script(fx_pass, program) or "")
                    .lower()
                    .replace(" ", "")
                    .find("draw=buffer")
                    >= 0
                    else "geometry"
                )
                draws.append(FxDraw(technique, fx_pass, subset, [], "", kind, order))
                order += 1
            continue
        target = ""
        stack = []
        for command in script.split(";"):
            if "=" not in command:
                continue
            key, value = command.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in ("rendercolortarget", "rendercolortarget0"):
                target = value
            elif key == "loopbycount":
                stack.append([value, fx_global_int(program, value, values), None])
            elif key == "loopgetindex":
                if stack:
                    stack[-1][2] = value
            elif key == "loopend":
                if stack:
                    stack.pop()
            elif key == "pass":
                fx_pass = passes.get(value)
                if fx_pass is None:
                    continue
                pass_script = (
                    (fx_pass_script(fx_pass, program) or "").lower().replace(" ", "")
                )
                kind = "buffer" if "draw=buffer" in pass_script else "geometry"
                loops = [(index or "", count, name) for name, count, index in stack]
                draws.append(
                    FxDraw(technique, fx_pass, subset, loops, target, kind, order)
                )
                order += 1
    return draws


def fx_find_function(program, name, argc):
    for function in program.functions.get(name, []):
        uniform = [p for p in function.params if "uniform" in p.quals]
        if len(uniform) >= argc or len(function.params) >= argc:
            return function
    return None


def fx_entry(program, compiled):
    if compiled is None:
        return None, []
    _tag, profile, call = compiled
    if call[0] != "call":
        return None, []
    function = fx_find_function(program, call[1], len(call[2]))
    return function, call[2]


def fx_bind_entry(function, compile_args):
    uniform = [i for i, p in enumerate(function.params) if "uniform" in p.quals]
    if not uniform and compile_args:
        uniform = list(
            range(len(function.params) - len(compile_args), len(function.params))
        )
    bound = {}
    for slot, arg in zip(uniform, compile_args):
        bound[slot] = arg
    return bound


def fx_norm_semantic(semantic):
    if not semantic:
        return None
    s = semantic.upper()
    if s.startswith("SV_"):
        s = s[3:]
    if s in (
        "COLOR",
        "TEXCOORD",
        "POSITION",
        "NORMAL",
        "TANGENT",
        "BINORMAL",
        "BLENDWEIGHT",
        "BLENDINDICES",
        "TARGET",
    ):
        s = s + "0"
    if s == "TARGET0":
        s = "COLOR0"
    return s


def fx_run_entry(ex, function, compile_args, inputs):
    program = ex.program
    bound = fx_bind_entry(function, compile_args)
    args = []
    for index, param in enumerate(function.params):
        pt = fx_resolve_type(param.type, program)
        if index in bound:
            args.append(ex.convert(ex.eval(bound[index]), pt))
            continue
        if "out" in param.quals and "inout" not in param.quals:
            args.append(None)
            continue
        if isinstance(pt, FxStruct):
            members = {}
            for name, type_name, semantic, _array in pt.members:
                mt = fx_resolve_type(type_name, program)
                members[name] = ex.convert(inputs(fx_norm_semantic(semantic), mt), mt)
            args.append(FxValue(pt, members))
        else:
            args.append(ex.convert(inputs(fx_norm_semantic(param.semantic), pt), pt))
        if ex.B.wants_text:
            ex.tag(args[-1], param.name)
    result, outs = ex.run_function(function, args)
    outputs = {}
    rt = fx_resolve_type(function.ret, program)
    if isinstance(rt, FxStruct):
        for name, type_name, semantic, _array in rt.members:
            sem = fx_norm_semantic(semantic)
            if sem:
                outputs[sem] = result.c[name]
    elif function.semantic and rt != "void":
        outputs[fx_norm_semantic(function.semantic)] = result
    for param in function.params:
        if param.name in outs:
            value = outs[param.name]
            if isinstance(value.t, FxStruct):
                for name, type_name, semantic, _array in value.t.members:
                    sem = fx_norm_semantic(semantic)
                    if sem:
                        outputs[sem] = value.c[name]
            elif param.semantic:
                outputs[fx_norm_semantic(param.semantic)] = value
    return outputs


def fx_lh_perspective(fov_y, aspect, near, far):
    ys = 1.0 / math.tan(fov_y * 0.5)
    xs = ys / aspect
    q = far / (far - near)
    return np.array(
        [[xs, 0, 0, 0], [0, ys, 0, 0], [0, 0, q, 1], [0, 0, -near * q, 0]],
        dtype=np.float64,
    )


def fx_lh_look(eye, forward, up):
    f = np.asarray(forward, dtype=np.float64)
    f = f / (np.linalg.norm(f) or 1.0)
    u = np.asarray(up, dtype=np.float64)
    r = np.cross(u, f)
    if np.linalg.norm(r) < 1e-9:
        r = np.cross(np.array([0.0, 0.0, 1.0]), f)
    r = r / (np.linalg.norm(r) or 1.0)
    u = np.cross(f, r)
    e = np.asarray(eye, dtype=np.float64)
    return np.array(
        [
            [r[0], u[0], f[0], 0],
            [r[1], u[1], f[1], 0],
            [r[2], u[2], f[2], 0],
            [-r.dot(e), -u.dot(e), -f.dot(e), 1],
        ],
        dtype=np.float64,
    )


def fx_lh_ortho(width, height, near, far):
    return np.array(
        [
            [2.0 / width, 0, 0, 0],
            [0, 2.0 / height, 0, 0],
            [0, 0, 1.0 / (far - near), 0],
            [0, 0, -near / (far - near), 1],
        ],
        dtype=np.float64,
    )


FX_NEAR = 1e-3


FX_FAR = 1e7


class FxImage:
    __slots__ = ("pixels", "width", "height", "path")

    def __init__(self, pixels, path):
        self.pixels = pixels
        self.height, self.width = pixels.shape[:2]
        self.path = path

    def sample(self, u, v, point, address):
        u = np.asarray(u, dtype=np.float64)
        v = np.asarray(v, dtype=np.float64)
        shape = np.broadcast(u, v).shape
        u = np.broadcast_to(u, shape)
        v = np.broadcast_to(v, shape)
        w, h = self.width, self.height

        def wrap(i, n):
            if address == "CLAMP":
                return np.clip(i, 0, n - 1)
            if address == "MIRROR":
                period = np.mod(i, 2 * n)
                return np.where(period >= n, 2 * n - 1 - period, period)
            return np.mod(i, n)

        if point:
            x = wrap(np.floor(np.nan_to_num(u) * w).astype(np.int64), w)
            y = wrap(np.floor(np.nan_to_num(v) * h).astype(np.int64), h)
            texel = self.pixels[y, x]
        else:
            fx = np.nan_to_num(u) * w - 0.5
            fy = np.nan_to_num(v) * h - 0.5
            x0 = np.floor(fx)
            y0 = np.floor(fy)
            tx = (fx - x0)[..., None]
            ty = (fy - y0)[..., None]
            x0 = x0.astype(np.int64)
            y0 = y0.astype(np.int64)
            xa, xb = wrap(x0, w), wrap(x0 + 1, w)
            ya, yb = wrap(y0, h), wrap(y0 + 1, h)
            p = self.pixels
            texel = (p[ya, xa] * (1 - tx) + p[ya, xb] * tx) * (1 - ty) + (
                p[yb, xa] * (1 - tx) + p[yb, xb] * tx
            ) * ty
        texel = np.asarray(texel, dtype=np.float64)
        if texel.ndim == 1:
            return [np.float64(texel[k]) for k in range(4)]
        return [texel[..., k] for k in range(4)]


FX_MATRIX_BASES = (
    "WORLDVIEWPROJECTION",
    "VIEWPROJECTION",
    "WORLDVIEW",
    "PROJECTION",
    "WORLD",
    "VIEW",
)


FX_MATERIAL_SEMANTICS = {
    "DIFFUSE",
    "AMBIENT",
    "EMISSIVE",
    "SPECULAR",
    "SPECULARPOWER",
    "TOONCOLOR",
    "EDGECOLOR",
    "GROUNDSHADOWCOLOR",
}


FX_FLAG_DEFAULTS = {
    "parthf": 1.0,
    "transp": 0.0,
    "spadd": 0.0,
    "opadd": 0.0,
    "use_toon": 0.0,
    "use_spheremap": 0.0,
    "use_subtexture": 0.0,
    "use_selfshadow": 0.0,
}


def fx_split_matrix_semantic(semantic):
    for base in FX_MATRIX_BASES:
        if semantic.startswith(base):
            suffix = semantic[len(base) :]
            if suffix in ("", "INVERSE", "TRANSPOSE", "INVERSETRANSPOSE"):
                return base, suffix
    return None


def fx_classify_global(g, program):
    semantic = g.semantic
    obj = str(fx_annotation(g, "Object", program, "") or "").lower()
    if semantic:
        matrix = fx_split_matrix_semantic(semantic)
        if matrix is not None:
            return (
                "matrix",
                matrix[0],
                matrix[1],
                "light" if obj == "light" else "camera",
            )
        if semantic == "POSITION":
            return ("position", "light" if obj == "light" else "camera")
        if semantic == "DIRECTION":
            return ("direction", "light" if obj == "light" else "camera")
        if semantic in FX_MATERIAL_SEMANTICS:
            if obj == "light":
                return ("light_color", semantic)
            return ("material", semantic)
        if semantic == "TIME":
            return ("time",)
        if semantic == "ELAPSEDTIME":
            return ("elapsed",)
        if semantic in ("VIEWPORTPIXELSIZE", "VIEWPORTSIZE"):
            return ("viewport",)
        if semantic == "CONTROLOBJECT":
            name = fx_annotation(g, "name", program, "") or ""
            item = fx_annotation(g, "item", program, "") or ""
            return ("control", str(name), str(item))
        if semantic in (
            "MOUSEPOSITION",
            "LEFTMOUSEDOWN",
            "MIDDLEMOUSEDOWN",
            "RIGHTMOUSEDOWN",
        ):
            return ("zero",)
        return None
    if g.init is None and g.name in FX_FLAG_DEFAULTS:
        return ("flag", g.name)
    if g.init is None and g.name == "use_texture":
        return ("flag", g.name)
    if g.name in getattr(program, "params", {}):
        return ("param", g.name)
    return None


def fx_source_is_runtime(source):
    if source is None:
        return False
    return source[0] in (
        "matrix",
        "position",
        "direction",
        "light_color",
        "time",
        "elapsed",
        "viewport",
        "control",
        "param",
    )


class FxSceneState:
    def __init__(self):
        self.world = np.identity(4)
        self.view = fx_lh_look((0.0, 10.0, -45.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0))
        self.proj = fx_lh_perspective(math.radians(30.0), 16.0 / 9.0, FX_NEAR, FX_FAR)
        self.camera_pos = np.array([0.0, 10.0, -45.0])
        self.camera_dir = np.array([0.0, 0.0, 1.0])
        self.light_dir = np.array([-0.5, -1.0, 0.5])
        self.light_color = np.array([154.0, 154.0, 154.0]) / 255.0
        self.time = 0.0
        self.elapsed = 1.0 / 30.0
        self.viewport = (1920.0, 1080.0)
        self.controls = {}
        self.params = {}
        self.flags = dict(FX_FLAG_DEFAULTS)
        self.material = {
            "diffuse": (1.0, 1.0, 1.0, 1.0),
            "ambient": (1.0, 1.0, 1.0),
            "specular": (0.0, 0.0, 0.0),
            "power": 0.0,
            "emissive": (0.0, 0.0, 0.0),
            "texture": "",
        }

    def light_matrices(self):
        direction = self.light_dir / (np.linalg.norm(self.light_dir) or 1.0)
        eye = -direction * 1000.0
        view = fx_lh_look(eye, direction, (0.0, 1.0, 0.0))
        proj = fx_lh_ortho(400.0, 400.0, 1.0, 4000.0)
        return view, proj

    def matrix(self, base, suffix, target):
        if target == "light":
            view, proj = self.light_matrices()
        else:
            view, proj = self.view, self.proj
        world = self.world
        if base == "WORLD":
            m = world
        elif base == "VIEW":
            m = view
        elif base == "PROJECTION":
            m = proj
        elif base == "WORLDVIEW":
            m = world @ view
        elif base == "VIEWPROJECTION":
            m = view @ proj
        else:
            m = world @ view @ proj
        if "INVERSE" in suffix:
            try:
                m = np.linalg.inv(m)
            except np.linalg.LinAlgError:
                m = np.identity(4)
        if "TRANSPOSE" in suffix:
            m = m.T
        return [float(x) for x in np.asarray(m).reshape(-1)]

    def values(self, source, g):
        kind = source[0]
        if kind == "matrix":
            return self.matrix(source[1], source[2], source[3])
        if kind == "position":
            if source[1] == "light":
                d = self.light_dir / (np.linalg.norm(self.light_dir) or 1.0)
                return [float(x) for x in (-d * 1000.0)] + [1.0]
            return [float(x) for x in self.camera_pos] + [1.0]
        if kind == "direction":
            d = self.light_dir if source[1] == "light" else self.camera_dir
            d = d / (np.linalg.norm(d) or 1.0)
            return [float(x) for x in d] + [0.0]
        if kind == "light_color":
            if source[1] == "DIFFUSE":
                return [0.0, 0.0, 0.0, 1.0]
            return [float(x) for x in self.light_color] + [1.0]
        if kind == "material":
            m = self.material
            sem = source[1]
            if sem == "DIFFUSE":
                return list(m["diffuse"])
            if sem == "AMBIENT":
                return list(m["ambient"]) + [1.0]
            if sem == "EMISSIVE":
                return list(m["emissive"]) + [1.0]
            if sem == "SPECULAR":
                return list(m["specular"]) + [1.0]
            if sem == "SPECULARPOWER":
                return [float(m["power"])]
            if sem == "TOONCOLOR":
                return [1.0, 1.0, 1.0, 1.0]
            if sem == "EDGECOLOR":
                return [0.0, 0.0, 0.0, 1.0]
            return [0.0, 0.0, 0.0, 0.5]
        if kind == "time":
            return [float(self.time)]
        if kind == "elapsed":
            return [float(self.elapsed)]
        if kind == "viewport":
            return [float(self.viewport[0]), float(self.viewport[1])]
        if kind == "control":
            found = self.controls.get(g.name)
            if found is None:
                return [0.0]
            return list(found)
        if kind == "flag":
            if source[1] == "use_texture":
                return [1.0 if self.material.get("texture") else 0.0]
            return [float(self.flags.get(source[1], 0.0))]
        if kind == "param":
            return list(self.params.get(source[1], [0.0]))
        return [0.0]


def fx_value_from_list(ex, values, t):
    comps = [ex.B.const(float(v)) for v in values]
    if not fx_is_numeric(t):
        return None
    n = fx_count(t[1])
    if len(t[1]) == 2 and len(comps) == 16 and t[1] != (4, 4):
        full = FxValue(("f", (4, 4)), comps)
        return ex.convert(full, t)
    if len(comps) == 1 and n > 1:
        comps = comps * n
    comps = (comps + [ex.B.const(0.0)] * n)[:n]
    return ex.convert(FxValue(("f", t[1]), comps), t)


class FxTextureStore:
    def __init__(self, program, loader):
        self.program = program
        self.loader = loader
        self.images = {}

    def texture_global(self, info):
        if info is None:
            return None
        name = info.texture
        if not name:
            return None
        return self.program.globals.get(name)

    def resource_path(self, g, material_texture=""):
        if g is None:
            return material_texture or None
        if g.semantic in ("MATERIALTEXTURE",):
            return material_texture or None
        if g.semantic and g.semantic not in ("",):
            if g.semantic.startswith(
                (
                    "RENDERCOLORTARGET",
                    "RENDERDEPTHSTENCILTARGET",
                    "OFFSCREENRENDERTARGET",
                    "ANIMATEDTEXTURE",
                    "MATERIALSPHEREMAP",
                    "MATERIALTOONTEXTURE",
                )
            ):
                return None
        resource = fx_annotation(g, "ResourceName", self.program, None)
        if not isinstance(resource, str) or not resource:
            return None
        return fx_find_file(self.program.folder, resource)

    def image(self, path):
        if path is None:
            return None
        if path not in self.images:
            try:
                self.images[path] = self.loader(path)
            except Exception:
                self.images[path] = None
        return self.images[path]


def fx_find_file(folder, name):
    name = name.replace("\\", "/")
    candidate = os.path.join(folder, name)
    if os.path.isfile(candidate):
        return candidate
    parent = os.path.dirname(candidate)
    base = os.path.basename(candidate).lower()
    stem = os.path.splitext(base)[0]
    if os.path.isdir(parent):
        entries = os.listdir(parent)
        for entry in entries:
            if entry.lower() == base:
                return os.path.join(parent, entry)
        for entry in entries:
            if os.path.splitext(entry)[0].lower() == stem:
                return os.path.join(parent, entry)
    return None


def fx_sampler_texture_global(program, info):
    if info is None or not isinstance(info, FxSampler):
        return None
    if info.texture:
        return program.globals.get(info.texture)
    return None


def fx_is_render_target(g):
    return (
        g is not None
        and g.semantic is not None
        and g.semantic.startswith(
            ("RENDERCOLORTARGET", "OFFSCREENRENDERTARGET", "RENDERDEPTHSTENCILTARGET")
        )
    )


class FxNumpyEnv:
    def __init__(self, program, state, textures):
        self.program = program
        self.state = state
        self.textures = textures
        self.sources = {}

    def source(self, g):
        if g.name not in self.sources:
            self.sources[g.name] = fx_classify_global(g, self.program)
        return self.sources[g.name]

    def global_value(self, ex, g):
        source = self.source(g)
        if source is None:
            return None
        t = fx_resolve_type(g.type, self.program)
        if not fx_is_numeric(t):
            return None
        return fx_value_from_list(ex, self.state.values(source, g), t)

    def sample(self, ex, info, u, v, lod):
        g = fx_sampler_texture_global(self.program, info)
        if fx_is_render_target(g):
            return [ex.B.const(0.0)] * 4
        material_texture = self.state.material.get("texture", "")
        if g is None and info is not None and info.texture is None:
            path = (
                material_texture
                if (info.register or "").lower().replace(" ", "") in ("s0", "")
                else None
            )
        else:
            path = self.textures.resource_path(g, material_texture)
        image = self.textures.image(path)
        if image is None:
            return [ex.B.const(0.0)] * 4
        return image.sample(
            u, v, info.point() if info else False, info.address() if info else "WRAP"
        )


def fx_pil_loader(path):
    from PIL import Image

    with Image.open(path) as handle:
        rgba = handle.convert("RGBA")
        data = np.asarray(rgba, dtype=np.float64) / 255.0
    return FxImage(data, path)


def fx_vertex_inputs(positions, normals, uvs, indices, colors=None):
    def provide(ex):
        B = ex.B

        def get(semantic, t):
            n = len(positions)
            if semantic == "POSITION0":
                comps = [positions[:, 0], positions[:, 1], positions[:, 2], np.ones(n)]
            elif semantic == "NORMAL0":
                comps = [normals[:, 0], normals[:, 1], normals[:, 2], np.zeros(n)]
            elif semantic == "TEXCOORD0":
                comps = [uvs[:, 0], uvs[:, 1], np.zeros(n), np.ones(n)]
            elif semantic in ("_INDEX", "INDEX"):
                comps = [indices.astype(np.float64)]
            elif semantic == "COLOR0" and colors is not None:
                comps = [colors[:, k] for k in range(4)]
            elif semantic in ("COLOR0", "COLOR1"):
                comps = [np.ones(n)] * 4
            else:
                comps = [np.zeros(n), np.zeros(n), np.zeros(n), np.ones(n)]
            if not fx_is_numeric(t):
                return ex.zero(t)
            count = fx_count(t[1])
            comps = (comps + [np.zeros(n)] * 16)[:count]
            return FxValue(("f", t[1]), comps)

        return get

    return provide


def fx_clip_to_world(clip, view_proj_inverse, proj):
    x, y, z, w = clip
    a = proj[2, 2]
    b = proj[3, 2]
    w = np.asarray(w, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    tiny = np.abs(w) <= 1e-12
    natural = a * w + b
    front = w > 1e-12
    outside = front & (
        (z < -1e-6 * np.maximum(1.0, np.abs(w))) | (z > w * (1 + 1e-9) + 1e-6)
    )
    behind = (w < -1e-12) & (np.abs(z - natural) > 1e-4 * (np.abs(w) + 1.0))
    culled = tiny | outside | behind
    homo = np.stack(
        [
            np.asarray(x, dtype=np.float64) * np.ones_like(w),
            np.asarray(y, dtype=np.float64) * np.ones_like(w),
            natural,
            w,
        ],
        axis=-1,
    )
    world = homo @ view_proj_inverse
    ww = world[:, 3:4]
    ww = np.where(np.abs(ww) < 1e-20, 1.0, ww)
    pos = world[:, :3] / ww
    pos = np.where(np.isfinite(pos), pos, 0.0)
    culled = culled | ~np.isfinite(world).all(axis=1)
    return pos, culled


FX_NATIVE_INTRINSICS = {
    "cross",
    "normalize",
    "length",
    "dot",
    "distance",
    "reflect",
    "lerp",
    "smoothstep",
    "saturate",
    "clamp",
}


FX_GROUP_INTRINSICS = {
    "mul",
    "smoothstep",
    "lerp",
    "normalize",
    "length",
    "distance",
    "cross",
    "dot",
    "reflect",
    "refract",
    "faceforward",
    "determinant",
}


def fx_stable(node):
    if isinstance(node, (tuple, list)):
        return "(" + ",".join(fx_stable(item) for item in node) + ")"
    if node is None or isinstance(node, (str, int, float, bool)):
        return repr(node)
    name = getattr(node, "name", None)
    return "<%s:%s>" % (type(node).__name__, name)


def fx_type_name(t):
    base = {"f": "float", "i": "int", "b": "bool"}.get(t[0], "float")
    dims = t[1]
    if not dims:
        return base
    if len(dims) == 1:
        return "%s%d" % (base, dims[0])
    return "%s%dx%d" % (base, dims[0], dims[1])


def fx_comp_label(name, t, k):
    dims = t[1]
    if not dims:
        return name
    if len(dims) == 2:
        return "%s[%d][%d]" % (name, k // dims[1], k % dims[1])
    if dims[0] <= 4:
        return "%s.%s" % (name, "xyzw"[k])
    return "%s[%d]" % (name, k)


def fx_sampler_slot(program, g):
    if g is None or g.init is None or g.init[0] != "sampler_state":
        return None
    tex = g.init[1].get("texture")
    if not tex:
        return None
    return "".join(tex).strip("<>() ")


def fx_global_constant(program, name, seen=None):
    seen = set() if seen is None else seen
    if name in seen:
        return True
    seen.add(name)
    g = program.globals.get(name)
    if g is None:
        return True
    if name in set(fx_script_values(program, "loopgetindex")):
        return False
    if fx_classify_global(g, program) is not None:
        return False
    ids = set()

    def walk(node):
        if isinstance(node, tuple) and node:
            if node[0] == "id" and len(node) > 1 and isinstance(node[1], str):
                ids.add(node[1])
            for item in node[1:]:
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(g.init)
    walk(g.array)
    return all(
        fx_global_constant(program, other, seen)
        for other in ids
        if other in program.globals
    )


def fx_function_info(program, function):
    cache = program.__dict__.setdefault("_fn_info", {})
    found = cache.get(id(function))
    if found is not None:
        return found
    info = {"pure": False, "hash": "", "slots": []}
    cache[id(function)] = info
    params = {p.name for p in function.params}
    local_names, ids, calls, flags = set(), set(), [], set()

    def walk(node):
        if isinstance(node, tuple) and node:
            kind = node[0]
            if kind == "decl":
                for name, _array, _init in node[2]:
                    local_names.add(name)
            elif kind == "id" and len(node) > 1 and isinstance(node[1], str):
                ids.add(node[1])
            elif kind == "call" and len(node) > 1 and isinstance(node[1], str):
                calls.append(node[1])
            elif kind == "discard":
                flags.add("discard")
            for item in node[1:]:
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(function.body)
    for p in function.params:
        walk(p.default)
    pure = "discard" not in flags
    deps = []
    slots = []
    for name in sorted(ids - params - local_names):
        g = program.globals.get(name)
        if g is None:
            continue
        try:
            t = fx_resolve_type(g.type, program)
        except FxError:
            pure = False
            break
        if t == "sampler":
            slot = fx_sampler_slot(program, g)
            if slot is None:
                pure = False
                break
            slots.append(slot)
            deps.append(("sampler", name, fx_stable(g.init)))
        elif t == "texture":
            slots.append(name)
            deps.append(("texture", name))
        elif fx_global_constant(program, name):
            deps.append(
                (
                    "const",
                    name,
                    fx_stable(g.type),
                    fx_stable(g.init),
                    fx_stable(g.array),
                    fx_stable(g.quals),
                )
            )
        else:
            pure = False
            break
    nested = []
    for cname in calls:
        for other in program.functions.get(cname, []):
            sub = fx_function_info(program, other)
            if not sub["pure"]:
                pure = False
            nested.append(sub["hash"])
            slots.extend(sub["slots"])
    signature = (
        function.name,
        [
            (
                p.name,
                fx_stable(p.type),
                sorted(p.quals),
                fx_stable(p.array),
                fx_stable(p.default),
            )
            for p in function.params
        ],
        fx_stable(function.ret),
        fx_stable(function.body),
        deps,
        sorted(nested),
        MME_CORE_VERSION,
    )
    info["pure"] = pure
    info["hash"] = hashlib.md5(fx_stable(signature).encode("utf-8")).hexdigest()
    info["slots"] = sorted(set(slots))
    return info


FX_DUMMY_FUNCTION = FxFunction("__intrinsic", "float", None, [], ("block", []))


def fx_intrinsic_values(ex, name, values):
    pushed = False
    if not ex.frames:
        ex.frames.append(FxFrame(FX_DUMMY_FUNCTION))
        pushed = True
    frame = ex.frames[-1]
    frame.scopes.append({"__a%d" % i: v for i, v in enumerate(values)})
    try:
        return FX_INTRINSICS[name](
            ex, [("id", "__a%d" % i) for i in range(len(values))]
        )
    finally:
        frame.scopes.pop()
        if pushed:
            ex.frames.pop()


FX_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    ">": 7,
    "<=": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}


def fx_operand_text(node, parent, right=False):
    text = fx_expr_text(node)
    if not isinstance(node, tuple) or not node:
        return text
    if node[0] in ("assign", "tern", "comma"):
        return "(%s)" % text
    if node[0] == "bin":
        mine = FX_PRECEDENCE.get(node[1], 11)
        outer = FX_PRECEDENCE.get(parent, 11)
        if mine < outer or (right and mine == outer):
            return "(%s)" % text
    return text


def fx_expr_text(node):
    if not isinstance(node, tuple) or not node:
        return "?"
    kind = node[0]
    if kind == "num":
        value = node[1]
        if isinstance(value, float) and value.is_integer() and abs(value) < 1e9:
            return str(int(value))
        return "%.7g" % value
    if kind == "str":
        return '"%s"' % node[1]
    if kind == "id":
        return node[1]
    if kind == "bin":
        return "%s %s %s" % (
            fx_operand_text(node[2], node[1]),
            node[1],
            fx_operand_text(node[3], node[1], True),
        )
    if kind == "un":
        return "%s%s" % (node[1], fx_operand_text(node[2], "unary"))
    if kind == "pre":
        return "%s%s" % (node[1], fx_expr_text(node[2]))
    if kind == "post":
        return "%s%s" % (fx_expr_text(node[2]), node[1])
    if kind == "assign":
        return "%s %s %s" % (fx_expr_text(node[2]), node[1], fx_expr_text(node[3]))
    if kind == "tern":
        return "%s ? %s : %s" % (
            fx_expr_text(node[1]),
            fx_expr_text(node[2]),
            fx_expr_text(node[3]),
        )
    if kind == "cast":
        return "(%s)%s" % (
            node[1] if isinstance(node[1], str) else "type",
            fx_expr_text(node[2]),
        )
    if kind in ("ctor", "call"):
        name = node[1] if isinstance(node[1], str) else "type"
        return "%s(%s)" % (name, ", ".join(fx_expr_text(a) for a in node[2]))
    if kind == "member":
        return "%s.%s" % (fx_expr_text(node[1]), node[2])
    if kind == "index":
        return "%s[%s]" % (fx_expr_text(node[1]), fx_expr_text(node[2]))
    if kind == "mcall":
        return "%s.%s(%s)" % (
            fx_expr_text(node[1]),
            node[2],
            ", ".join(fx_expr_text(a) for a in node[3]),
        )
    if kind == "comma":
        return ", ".join(fx_expr_text(a) for a in node[1])
    if kind == "init":
        return "{%s}" % ", ".join(fx_expr_text(a) for a in node[1])
    return kind


def fx_stmt_text(node):
    kind = node[0]
    if kind == "expr":
        text = fx_expr_text(node[1])
    elif kind == "decl":
        parts = []
        for name, _array, init in node[2]:
            parts.append(
                name if init is None else "%s = %s" % (name, fx_expr_text(init))
            )
        type_name = (
            node[1] if isinstance(node[1], str) else getattr(node[1], "name", "struct")
        )
        text = "%s %s" % (type_name, ", ".join(parts))
    elif kind == "return":
        text = "return" if node[1] is None else "return %s" % fx_expr_text(node[1])
    elif kind == "if":
        text = "if (%s)" % fx_expr_text(node[1])
    elif kind == "for":
        text = "for (%s)" % (fx_expr_text(node[2]) if node[2] is not None else "")
    elif kind == "do":
        text = "while (%s)" % fx_expr_text(node[2])
    else:
        return None
    return text if len(text) <= MME_LABEL_LIMIT else text[: MME_LABEL_LIMIT - 1] + "…"


MME_LABEL_LIMIT = 64
