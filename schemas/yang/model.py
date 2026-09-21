"""A small YANG reader: enough of the language to validate data paths.

Not a YANG compiler. It reads the published IEEE and IETF modules in this
directory and answers one question: does this instance-data path exist in the
standard? That question is worth a tool because getting it wrong is invisible.
A configuration emitter can produce a well-formed document full of node names
that no model defines, push it at a device, and see it rejected with a generic
error -- or worse, silently ignored.

This was written after exactly that happened: the FRER emitter here addressed
`/ieee802-dot1cb-frer:frame-replication-and-elimination/...`, a container that
does not exist. The real one is `frer`. Nothing in the code or the tests could
have caught it, because nothing compared the output to the model.

WHAT IS SUPPORTED
  module / submodule headers, prefix and import resolution
  container, list, leaf, leaf-list, choice, case, grouping, uses, augment
  `uses` expansion across imported modules
  `augment` applied to an absolute schema path

WHAT IS NOT
  when/must/if-feature evaluation (a node behind a false `when` still resolves)
  type checking, defaults, ranges, deviations
  submodule `include` beyond treating it as another file in the directory

Both lists are honest limits, not a roadmap. Path existence is what the
conformance test needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Statements that carry a data node into the tree.
DATA_KEYWORDS = {"container", "list", "leaf", "leaf-list", "anydata", "anyxml", "notification"}
# Statements that are transparent: their children belong to the parent.
TRANSPARENT = {"choice", "case", "input", "output"}


@dataclass
class Stmt:
    keyword: str
    arg: str | None
    children: list["Stmt"] = field(default_factory=list)

    def find(self, kw: str) -> list["Stmt"]:
        return [c for c in self.children if c.keyword == kw]

    def first(self, kw: str) -> "Stmt | None":
        for c in self.children:
            if c.keyword == kw:
                return c
        return None


def _strip(text: str) -> str:
    """Remove comments, being careful not to eat them inside quoted strings."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1]); i = j + 1
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2
        else:
            out.append(c); i += 1
    return "".join(out)


_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[{};]|[^\s{};"\']+')


def parse(text: str) -> Stmt:
    """Parse one module into a statement tree."""
    toks = _TOKEN.findall(_strip(text))
    pos = 0

    def unquote(t: str) -> str:
        if t.startswith('"'):
            return re.sub(r"\\(.)", r"\1", t[1:-1])
        if t.startswith("'"):
            return t[1:-1]
        return t

    def take_stmt() -> Stmt | None:
        nonlocal pos
        if pos >= len(toks):
            return None
        kw = toks[pos]; pos += 1
        if kw in "{};":
            return None
        args = []
        while pos < len(toks) and toks[pos] not in "{};":
            args.append(unquote(toks[pos])); pos += 1
            # YANG string concatenation: "a" + "b"
            if pos < len(toks) and toks[pos] == "+":
                pos += 1
                continue
        arg = " ".join(args) if args else None
        node = Stmt(kw, arg)
        if pos < len(toks) and toks[pos] == "{":
            pos += 1
            while pos < len(toks) and toks[pos] != "}":
                child = take_stmt()
                if child is not None:
                    node.children.append(child)
            pos += 1                      # closing brace
        elif pos < len(toks) and toks[pos] == ";":
            pos += 1
        return node

    root = take_stmt()
    if root is None:
        raise ValueError("empty module")
    return root


@dataclass
class Node:
    """A resolved data node."""
    name: str
    keyword: str
    module: str
    children: dict[str, "Node"] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)

    @property
    def is_list(self) -> bool:
        return self.keyword == "list"

    @property
    def is_leaf(self) -> bool:
        return self.keyword in ("leaf", "leaf-list")


class YangSet:
    """Every module in a directory tree, resolved into one data tree."""

    def __init__(self, roots: list[Path] | None = None):
        self.modules: dict[str, Stmt] = {}
        self.prefix_of: dict[str, str] = {}        # module name -> its own prefix
        self.groupings: dict[str, dict[str, Stmt]] = {}
        self.top: dict[str, Node] = {}             # "module:name" -> Node
        self._by_module_name: dict[tuple[str, str], Node] = {}
        for d in roots or [HERE]:
            for f in sorted(Path(d).rglob("*.yang")):
                try:
                    st = parse(f.read_text(errors="ignore"))
                except Exception:
                    continue
                if st.keyword not in ("module", "submodule") or not st.arg:
                    continue
                self.modules[st.arg] = st
        self._index()
        self._build()

    # -- indexing -----------------------------------------------------------
    def _index(self) -> None:
        for name, mod in self.modules.items():
            p = mod.first("prefix")
            self.prefix_of[name] = p.arg if p and p.arg else name
            self.groupings[name] = {g.arg: g for g in mod.find("grouping") if g.arg}

    def _imports(self, module: str) -> dict[str, str]:
        """prefix -> module name, including the module's own prefix."""
        out = {self.prefix_of[module]: module}
        for imp in self.modules[module].find("import"):
            p = imp.first("prefix")
            if imp.arg and p and p.arg:
                out[p.arg] = imp.arg
        for inc in self.modules[module].find("include"):
            if inc.arg in self.modules:
                out.update(self._imports(inc.arg)) if inc.arg != module else None
        return out

    def _grouping(self, module: str, ref: str) -> tuple[str, Stmt] | None:
        if ":" in ref:
            pfx, name = ref.split(":", 1)
            target = self._imports(module).get(pfx)
            if target and name in self.groupings.get(target, {}):
                return target, self.groupings[target][name]
            return None
        if ref in self.groupings.get(module, {}):
            return module, self.groupings[module][ref]
        # submodules of the same family
        for inc in self.modules[module].find("include"):
            if inc.arg and ref in self.groupings.get(inc.arg, {}):
                return inc.arg, self.groupings[inc.arg][ref]
        return None

    # -- tree building -------------------------------------------------------
    def _expand(self, module: str, stmts: list[Stmt], into: dict[str, Node],
                depth: int = 0) -> None:
        if depth > 40:
            return
        for st in stmts:
            if st.keyword == "uses" and st.arg:
                found = self._grouping(module, st.arg)
                if found:
                    gmod, g = found
                    self._expand(gmod, g.children, into, depth + 1)
                # `uses X { augment "child/path" { ... } }` adds nodes to the
                # instantiated grouping. IEEE uses this for the single most
                # important leaf in the whole schedule -- gate-states-value is
                # attached to gate-control-entry exactly this way -- so a
                # reader that skips it concludes the standard has no such node.
                for aug in st.find("augment"):
                    if not aug.arg:
                        continue
                    target = into
                    node = None
                    for seg in [x for x in aug.arg.split("/") if x]:
                        node = target.get(seg.split(":")[-1])
                        if node is None:
                            break
                        target = node.children
                    if node is not None:
                        self._expand(module, aug.children, node.children, depth + 1)
                continue
            if st.keyword in TRANSPARENT:
                self._expand(module, st.children, into, depth + 1)
                continue
            if st.keyword not in DATA_KEYWORDS or not st.arg:
                continue
            node = Node(name=st.arg, keyword=st.keyword, module=module)
            k = st.first("key")
            if k and k.arg:
                node.keys = k.arg.split()
            if st.keyword in ("container", "list", "notification"):
                self._expand(module, st.children, node.children, depth + 1)
            into[node.name] = node
            self._by_module_name[(module, node.name)] = node

    def _build(self) -> None:
        for name, mod in self.modules.items():
            if mod.keyword != "module":
                continue
            self._expand(name, mod.children, self.top_for(name))
        # augments last: their targets must already exist
        for name, mod in self.modules.items():
            for aug in mod.find("augment"):
                if not aug.arg or not aug.arg.startswith("/"):
                    continue
                target = self._walk_schema(name, aug.arg)
                if target is not None:
                    self._expand(name, aug.children, target.children)

    def top_for(self, module: str) -> dict[str, Node]:
        return self.top.setdefault(module, {})

    def _walk_schema(self, module: str, path: str) -> Node | None:
        """Follow an absolute schema path like /if:interfaces/if:interface/dot1q:bridge-port."""
        imports = self._imports(module)
        cur: dict[str, Node] | None = None
        node: Node | None = None
        for part in [p for p in path.split("/") if p]:
            pfx, _, name = part.partition(":")
            if not name:
                name, pfx = pfx, None
            mod = imports.get(pfx) if pfx else module
            if cur is None:
                if mod is None:
                    return None
                node = self.top.get(mod, {}).get(name)
                # a top-level node may have been contributed by another module
                if node is None:
                    node = self._by_module_name.get((mod, name))
            else:
                node = cur.get(name)
            if node is None:
                return None
            cur = node.children
        return node

    # -- the public question -------------------------------------------------
    def resolve(self, path: str) -> tuple[bool, str]:
        """Does this instance-data path exist? Returns (ok, explanation).

        Accepts paths with list predicates, e.g.
            /ietf-interfaces:interfaces/interface[name='1']/...
        Every segment may be prefixed with a module NAME (not a prefix), which
        is how instance-identifiers are written.
        """
        parts = [p for p in path.split("/") if p]
        if not parts:
            return False, "empty path"
        cur: dict[str, Node] | None = None
        node: Node | None = None
        walked: list[str] = []
        for part in parts:
            bare = re.sub(r"\[.*?\]", "", part)
            preds = re.findall(r"\[\s*([\w:-]+)\s*=", part)
            mod, _, name = bare.partition(":")
            if not name:
                name, mod = mod, ""
            if cur is None:
                if not mod:
                    return False, f"first segment {part!r} must name its module"
                if mod not in self.modules:
                    return False, f"unknown module {mod!r}"
                node = self.top.get(mod, {}).get(name) or self._by_module_name.get((mod, name))
                if node is None:
                    return False, f"{mod} has no top-level node {name!r}"
            else:
                node = cur.get(name)
                if node is None:
                    near = ", ".join(sorted(cur)[:8]) or "(no children)"
                    return False, (f"no node {name!r} under /{'/'.join(walked)}; "
                                   f"available: {near}")
                if mod and mod not in self.modules:
                    return False, f"unknown module {mod!r} on segment {part!r}"
            walked.append(name)
            if preds and not node.is_list:
                return False, f"{'/'.join(walked)} is a {node.keyword}, not a list, but carries a predicate"
            if node.is_list and preds:
                unknown = [p.split(":")[-1] for p in preds
                           if p.split(":")[-1] not in node.keys]
                if unknown:
                    return False, (f"{'/'.join(walked)} is keyed by "
                                   f"{', '.join(node.keys)}; got {', '.join(unknown)}")
            cur = node.children
        return True, f"{node.keyword} {'/'.join(walked)}"


_SET: YangSet | None = None


def standard_set() -> YangSet:
    """The published models shipped in this directory, parsed once."""
    global _SET
    if _SET is None:
        _SET = YangSet([HERE])
    return _SET
