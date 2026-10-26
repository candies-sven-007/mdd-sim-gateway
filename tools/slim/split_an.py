"""Analysis for splitting control/app/main.py.

segments(): partition main.py into top-level blocks (leading comments/blank lines belong to
the following block).  Each block: names it defines, module-level names it references
(scope-aware), route path if decorated with @app.<method>.
"""
import ast
import builtins
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "control/app/main.py"


class Block:
    def __init__(self, idx, node, start, end, text):
        self.idx, self.node, self.start, self.end, self.text = idx, node, start, end, text
        self.defines = set()
        self.refs = set()        # module-level names referenced from any scope
        self.global_writes = set()  # names written via `global` or module-level assignment
        self.route = None
        self.kind = type(node).__name__

    @property
    def name(self):
        return getattr(self.node, "name", None) or ",".join(sorted(self.defines)) or f"<{self.kind}@{self.start}>"


def _targets(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
    return out


class Scope:
    def __init__(self, kind, local, parent):
        self.kind, self.local, self.parent = kind, local, parent


def function_locals(fn):
    """Names bound locally in a function/lambda (not descending into nested scopes)."""
    local, glob, nonloc = set(), set(), set()
    args = fn.args
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        local.add(a.arg)
    if args.vararg: local.add(args.vararg.arg)
    if args.kwarg: local.add(args.kwarg.arg)
    body = fn.body if isinstance(fn.body, list) else [fn.body]

    def visit(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(n.name)
            for d in n.decorator_list: visit(d)
            if not isinstance(n, ast.ClassDef):
                for d in n.args.defaults + [x for x in n.args.kw_defaults if x]: visit(d)
            else:
                for b in n.bases + [k.value for k in n.keywords]: visit(b)
            return
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]: visit(d)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # first iterator evaluated in enclosing scope
            visit(n.generators[0].iter)
            return
        if isinstance(n, ast.Global):
            glob.update(n.names); return
        if isinstance(n, ast.Nonlocal):
            nonloc.update(n.names); return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            local.add(n.id)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                local.add((a.asname or a.name).split(".")[0])
        if isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        if isinstance(n, ast.NamedExpr):
            local.add(n.target.id)
        if isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            local.add(n.name)
        for c in ast.iter_child_nodes(n):
            visit(c)

    for s in body:
        visit(s)
    return (local - glob) - nonloc, glob


def comp_locals(n):
    out = set()
    for g in n.generators:
        out |= _targets(g.target)
    return out


def global_name_nodes(tree_node, module_names):
    """Yield (Name node, is_store) for every name occurrence in tree_node that resolves to
    module scope and is in module_names. tree_node is a top-level statement."""
    results = []

    def walk(n, scope):
        # scope: list of sets (innermost last) of local names for function/comp scopes;
        # class scopes are skipped for resolution of nested functions (Python semantics).
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list: walk(d, scope)
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]: walk(d, scope)
            # annotations are strings (from __future__ import annotations) -> ignore
            local, glob = function_locals(n)
            inner = [s for s in scope if s[0] != "class"] + [("func", local, glob)]
            for s in n.body: walk(s, inner)
            return
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]: walk(d, scope)
            local, glob = function_locals(n)
            inner = [s for s in scope if s[0] != "class"] + [("func", local, glob)]
            walk(n.body, inner)
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list + n.bases + [k.value for k in n.keywords]: walk(d, scope)
            local = set()
            for s in n.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    local.add(s.name)
                elif isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    local |= _targets(s)
            inner = scope + [("class", local, set())]
            for s in n.body: walk(s, inner)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            walk(n.generators[0].iter, scope)
            inner = [s for s in scope if s[0] != "class" or s is scope[-1]] + [("comp", comp_locals(n), set())]
            # comprehension body cannot see class scope except first iter; drop class scopes
            inner = [s for s in scope if s[0] != "class"] + [("comp", comp_locals(n), set())]
            for i, g in enumerate(n.generators):
                walk(g.target, inner)
                if i: walk(g.iter, inner)
                for c in g.ifs: walk(c, inner)
            if isinstance(n, ast.DictComp):
                walk(n.key, inner); walk(n.value, inner)
            else:
                walk(n.elt, inner)
            return
        if isinstance(n, ast.Name):
            if n.id in module_names and resolves_global(n.id, scope):
                results.append((n, isinstance(n.ctx, (ast.Store, ast.Del))))
            return
        if isinstance(n, ast.Global):
            for name in n.names:
                if name in module_names:
                    results.append((n, True))
            return
        for c in ast.iter_child_nodes(n):
            walk(c, scope)

    def resolves_global(name, scope):
        # innermost first; class scope only counts if it is the innermost
        for i, (kind, local, glob) in enumerate(reversed(scope)):
            if name in glob:
                return True
            if kind == "class":
                if i == 0 and name in local:
                    return False
                continue
            if name in local:
                return False
        return True

    walk(tree_node, [])
    return results


def segments(src=None):
    src = src if src is not None else MAIN.read_text()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    blocks = []
    prev_end = 0
    for i, node in enumerate(tree.body):
        end = node.end_lineno
        blocks.append(Block(i, node, prev_end + 1, end, "".join(lines[prev_end:end])))
        prev_end = end
    tail = "".join(lines[prev_end:])
    module_names = set()
    for b in blocks:
        n = b.node
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b.defines.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                b.defines.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            b.defines |= _targets(n)
        elif isinstance(n, ast.If):
            for s in n.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    b.defines.add(s.name)
                elif isinstance(s, (ast.Assign, ast.AnnAssign)):
                    b.defines |= _targets(s)
        module_names |= b.defines
    for b in blocks:
        for name_node, store in global_name_nodes(b.node, module_names):
            if isinstance(name_node, ast.Global):
                b.global_writes |= set(name_node.names) & module_names
                continue
            if store and not isinstance(b.node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom, ast.If)):
                b.global_writes.add(name_node.id)
            b.refs.add(name_node.id)
        b.refs -= b.defines
        n = b.node
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    b.route = (d.func.attr, d.args[0].value)
    return blocks, module_names, tail


if __name__ == "__main__":
    blocks, names, tail = segments()
    for b in blocks:
        print(b.start, b.end, b.kind, b.name, b.route or "", sorted(b.refs)[:12], sorted(b.global_writes))
