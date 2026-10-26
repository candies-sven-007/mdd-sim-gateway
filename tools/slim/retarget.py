"""Retarget tests from control.app.main to the modules the split moved things to.
Temporary tool, never committed.  Usage: retarget.py [--check] test files..."""
import ast, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAYOUT = json.loads((Path(__file__).with_name("layout.json")).read_text())
NAMES, IMPORTS = LAYOUT["names"], LAYOUT["imports"]
MAIN_SRC = (ROOT / "control/app/main.py").read_text()
main_tree = ast.parse(MAIN_SRC)
MAIN_BOUND = set()
for n in main_tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        MAIN_BOUND.add(n.name)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            MAIN_BOUND.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.If):
        for s in n.body:
            if hasattr(s, "name"): MAIN_BOUND.add(s.name)
    for t in ast.walk(n) if isinstance(n, (ast.Assign, ast.AnnAssign)) else []:
        if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
            MAIN_BOUND.add(t.id)


def alias_for(mod):
    return f"app_{mod}" if mod in ("state", "tasks") else f"{mod}_routes"


def import_line_for_module(mod):
    if mod in ("state", "tasks"):
        return f"from control.app import {mod} as app_{mod}"
    return f"from control.app.routers import {mod} as {mod}_routes"


def import_for_original(name):
    spec = IMPORTS[name]
    if "import" in spec:
        return name, f"import {spec['import']}"
    src = spec["from"]
    if src.startswith("."):
        pkg = "control.app" + ("." + src.lstrip(".") if src.strip(".") else "")
        if src == ".":
            real = spec["name"]
            return name, (f"from control.app import {real}" if real == name
                          else f"from control.app import {real} as {name}")
        return name, f"from {pkg} import {spec['name']}" + ("" if spec["name"] == name else f" as {name}")
    return name, f"from {src} import {spec['name']}" + ("" if spec["name"] == name else f" as {name}")


def process(path, check):
    src = path.read_text()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    offs = [0]
    for l in lines: offs.append(offs[-1] + len(l))
    def pos(lineno, col):
        return offs[lineno - 1] + len(lines[lineno - 1].encode()[:col].decode())
    edits, needed, manual = [], set(), []
    bound_in_test = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): bound_in_test.add(n.id)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names: bound_in_test.add((a.asname or a.name).split(".")[0])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): bound_in_test.add(n.name)
        if isinstance(n, ast.arg): bound_in_test.add(n.arg)
    existing_imports = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            existing_imports.add(ast.get_source_segment(src, n))

    def target(attr):
        if attr in MAIN_BOUND:
            return None
        if attr in NAMES and NAMES[attr] not in ("main", "<import>"):
            m = NAMES[attr]; return alias_for(m), import_line_for_module(m)
        if attr in IMPORTS:
            return import_for_original(attr)
        return "?", None

    patched_objs = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "object"
                and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) and n.args[0].id == "main"
                and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            attr = n.args[1].value
            if attr in IMPORTS:
                manual.append(f"{path.name}:{n.lineno} patch.object(main, {attr!r}) rebinds an import")
                continue
            t = target(attr)
            if t is None: continue
            if t[0] == "?": manual.append(f"{path.name}:{n.lineno} unknown main.{attr}"); continue
            a = n.args[0]; edits.append((pos(a.lineno, a.col_offset), pos(a.end_lineno, a.end_col_offset), t[0])); needed.add(t)
            patched_objs.add(id(a))
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "main":
            t = target(n.attr)
            if t is None: continue
            if t[0] == "?": manual.append(f"{path.name}:{n.lineno} unknown main.{n.attr}"); continue
            if t[0] in bound_in_test and t[1] not in existing_imports and t[0] not in ("app_state", "app_tasks") and not t[0].endswith("_routes"):
                manual.append(f"{path.name}:{n.lineno} name clash {t[0]}"); continue
            v = n.value
            if t[0] in IMPORTS:  # main.cfg.x -> cfg.x
                edits.append((pos(v.lineno, v.col_offset), pos(n.end_lineno, n.end_col_offset), t[0]))
            else:
                edits.append((pos(v.lineno, v.col_offset), pos(v.end_lineno, v.end_col_offset), t[0]))
            needed.add(t)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("control.app.main."):
            attr = n.value.split(".")[3]
            t = target(attr)
            if t is None: continue
            manual.append(f"{path.name}:{n.lineno} string patch {n.value}")
    if check:
        for m in manual: print("MANUAL", m)
        return bool(edits)
    out = src
    for a, b, new in sorted(edits, reverse=True):
        out = out[:a] + new + out[b:]
    # imports: add after `from control.app import main` (or after last top-level import)
    add = sorted({t[1] for t in needed if t[1] and t[1] not in existing_imports})
    if add:
        m = re.search(r"^from control\.app import main\n", out, re.M)
        if m:
            out = out[:m.end()] + "".join(x + "\n" for x in add) + out[m.end():]
        else:
            last = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))][-1]
            idx = offs[last.end_lineno]
            out = out[:idx] + "".join(x + "\n" for x in add) + out[idx:]
    # drop `main` import if no longer used
    t2 = ast.parse(out)
    if edits and not any(isinstance(n, ast.Name) and n.id == "main" for n in ast.walk(t2)):
        out = re.sub(r"^from control\.app import main\n", "", out, flags=re.M)
        out = re.sub(r"^(from control\.app import .*)\bmain, ", r"\1", out, flags=re.M)
    path.write_text(out)
    for m in manual: print("MANUAL", m)
    return True


if __name__ == "__main__":
    check = "--check" in sys.argv
    files = [Path(x) for x in sys.argv[1:] if not x.startswith("--")] or sorted((ROOT / "tests").glob("test_*.py"))
    for f in files:
        if "main" not in f.read_text(): continue
        if process(f, check) and not check:
            print("rewrote", f.name)
