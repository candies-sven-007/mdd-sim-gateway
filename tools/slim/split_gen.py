"""Generate state.py / tasks.py / routers/*.py / main.py from the ORIGINAL main.py.

Usage: python split_gen.py ORIGINAL_MAIN STEP   (STEP = comma list of routers moved so far,
       or 'all').  Prints the assignment and writes files under control/app/.
Temporary tool: never committed.
"""
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import split_an  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "control/app"

ROUTER_OF_PATH = [
    ("/api/auth/", "auth"),
    ("/api/engine/event", "engine"),
    ("/api/esim/", "esim"),
    ("/api/egress/", "egress"),
    ("/api/settings", "system"),
    ("/api/system/", "system"),
    ("/ws", "system"),
    ("/api/diagnostics/", "logs"),
    ("/api/media", "softphone"),
]
SOFTPHONE_SUFFIXES = ("/softphone", "/softphone/ws", "/call", "/hangup", "/calls", "/calls/delete")
LINES_PREFIXES = ("/api/instances", "/api/provision", "/api/ports/", "/api/cards", "/api/readers",
                  "/api/sim/", "/api/devices")
ROUTER_ORDER = ["auth", "lines", "engine", "softphone", "esim", "egress", "logs", "system"]
TASK_ENTRIES = {"card_monitor", "status_poller", "host_health_poller", "media_supervisor"}
PER_MODULE = {"log"}   # each module defines its own logger of the same name
MAIN_FIXED = {"log", "lifespan", "app", "audit_mutations", "_audit_client", "_write_audit_record",
              "INDEX_CACHE_CONTROL", "ASSET_CACHE_CONTROL", "_HashedAssets", "spa", "WEBUI_DIR"}


def router_for(path):
    if path.startswith("/api/instances/{iid}"):
        rest = path[len("/api/instances/{iid}"):]
        if rest in SOFTPHONE_SUFFIXES:
            return "softphone"
        if rest == "/logs":
            return "logs"
    for prefix, name in ROUTER_OF_PATH:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return name
    if path.startswith(LINES_PREFIXES):
        return "lines"
    raise SystemExit(f"no router for {path}")


def module_path(mod):
    if mod in ("state", "tasks", "main"):
        return APP / f"{mod}.py"
    return APP / "routers" / f"{mod}.py"


def import_ref(mod):
    """How another control.app module refers to mod (routers get an alias: three of them share
    a name with a business module)."""
    if mod in ("state", "tasks"):
        return f"app_{mod}"
    return mod if mod == "main" else f"{mod}_routes"


USED = defaultdict(set)   # module -> modules it references


def assign(blocks, moved, extra_fixed=None):
    """name of block -> module. Unmoved things stay in 'main'."""
    by_name = {}
    for b in blocks:
        for d in b.defines:
            by_name.setdefault(d, b)
    mod = {}
    imports = set()
    for b in blocks:
        if b.route and b.route[0] != "middleware":
            b.refs.discard("app")
    for b in blocks:
        n = b.node
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mod[b.idx] = "<import>"; imports |= b.defines; continue
        imports |= b.defines & PER_MODULE
        if b.idx == 0:
            mod[b.idx] = "main"; continue
        if b.route and b.route[0] != "middleware":
            r = router_for(b.route[1])
            mod[b.idx] = r if r in moved else "main"
        elif b.defines & MAIN_FIXED or (isinstance(n, ast.Expr) and b.idx > 0 and "app" in b.refs):
            mod[b.idx] = "main"
        elif b.defines & TASK_ENTRIES:
            mod[b.idx] = "tasks" if "tasks" in moved else "main"
    if extra_fixed:
        for b in blocks:
            for d in b.defines:
                if d in extra_fixed:
                    mod[b.idx] = extra_fixed[d]
    # referencing blocks per symbol
    users = defaultdict(set)
    for b in blocks:
        for r in b.refs:
            if r in by_name and r not in imports:
                users[by_name[r].idx].add(b.idx)
    changed = True
    while changed:
        changed = False
        for b in blocks:
            if b.idx in mod:
                continue
            us = users[b.idx]
            if not us:
                mod[b.idx] = "main"; changed = True; continue
            if all(u in mod for u in us):
                mods = {mod[u] for u in us}
                if len(mods) == 1:
                    mod[b.idx] = mods.pop()
                elif mods == {"main"} or not mods - {"main"}:
                    mod[b.idx] = "main"
                else:
                    non_main = mods - {"main"}
                    mod[b.idx] = "state" if len(non_main) > 1 or "main" in mods else non_main.pop()
                    if "main" in mods and len(non_main) == 1:
                        mod[b.idx] = "state"
                changed = True
        if not changed:
            pending = [b for b in blocks if b.idx not in mod]
            if pending:
                # cycles: put the remaining helpers in state when any user is outside main
                for b in pending:
                    mod[b.idx] = "state"
                changed = True
    # closure: non-main modules may reference only themselves and state
    changed = True
    while changed:
        changed = False
        for b in blocks:
            m = mod[b.idx]
            if m in ("main", "<import>"):
                continue
            for r in b.refs:
                if r in imports or r not in by_name:
                    continue
                t = by_name[r]
                tm = mod[t.idx]
                if tm == m or tm == "state":
                    continue
                if t.route and t.route[0] != "middleware":
                    raise SystemExit(f"{b.name} ({m}) needs route {t.name} ({tm})")
                if t.defines & MAIN_FIXED:
                    raise SystemExit(f"{b.name} ({m}) needs main-only {t.name}")
                mod[t.idx] = "state"
                changed = True
    return mod, by_name, imports


def rewrite_block(b, src_lines, by_name, mod, imports, module_names):
    """Return block text with cross-module refs rewritten to module.name."""
    m = mod[b.idx]
    edits = []
    for node, store in split_an.global_name_nodes(b.node, module_names):
        if isinstance(node, ast.Global):
            names = [x for x in node.names if x in by_name and mod[by_name[x].idx] != m]
            if names:
                raise SystemExit(f"global of foreign {names} in {b.name}")
            continue
        name = node.id
        if (name == "app" and b.route and b.route[0] != "middleware"
                and node.lineno < b.node.lineno):
            continue
        if name in imports or name not in by_name:
            continue
        tm = mod[by_name[name].idx]
        if tm == m:
            continue
        if store:
            raise SystemExit(f"store to foreign {name} in {b.name}")
        USED[m].add(tm)
        edits.append((node.lineno, node.col_offset, node.end_col_offset, f"{import_ref(tm)}.{name}"))
    lines = src_lines[b.start - 1:b.end]
    lines = list(lines)
    for lineno, c0, c1, new in sorted(edits, reverse=True):
        i = lineno - b.start
        raw = lines[i].encode()
        lines[i] = (raw[:c0].decode() + new + raw[c1:].decode())
    text = "".join(lines)
    if b.route and b.route[0] != "middleware" and m not in ("main",):
        text = text.replace("@app.", "@router.", 1)
        assert "@app." not in text, b.name
    return text


def used_imports(texts, import_blocks, src_lines, extra_names=None):
    """Rebuild the original import statements keeping only names used in texts. Keeps the
    original grouping (blank lines) and wraps long `from x import (...)` lines."""
    import textwrap
    tree_names = set()
    for t in texts:
        for n in ast.walk(ast.parse(t)):
            if isinstance(n, ast.Name):
                tree_names.add(n.id)
    extra_names = extra_names or {}
    out = []
    for b in import_blocks:
        n = b.node
        if isinstance(n, ast.ImportFrom) and n.module == "__future__":
            continue
        keep = [a for a in n.names if (a.asname or a.name).split(".")[0] in tree_names]
        add = extra_names.get(getattr(n, "module", None), []) if isinstance(n, ast.ImportFrom) else []
        if not keep and not add:
            continue
        gap = "\n" if out and b.text.startswith("\n") else ""
        if len(keep) == len(n.names) and not add:
            seg = "".join(src_lines[n.lineno - 1:n.end_lineno])
            out.append(gap + seg.rstrip("\n") + "\n")
            continue
        parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in keep] + list(add)
        if isinstance(n, ast.ImportFrom):
            head = f"from {'.' * n.level}{n.module or ''} import "
            stmt = head + ", ".join(parts)
            if len(stmt) > 99:
                body = textwrap.fill(", ".join(parts), width=99 - len(head) - 1,
                                     break_long_words=False, break_on_hyphens=False)
                lines = body.split("\n")
                pad = " " * (len(head) + 1)
                stmt = head + "(" + ("\n" + pad).join(lines) + ")"
            out.append(gap + stmt + "\n")
        else:
            out.append(gap + f"import {', '.join(parts)}\n")
    return out, None


def group_local(imp):
    out = []
    for i, line in enumerate(imp):
        bare = line.lstrip("\n")
        if (bare.startswith("from .") and out and not out[-1].lstrip("\n").startswith("from .")
                and not line.startswith("\n")):
            line = "\n" + line
        out.append(line)
    return out


def relative(mod_from, line):
    """Imports in routers/ need one more dot for package-relative imports."""
    if mod_from in ("state", "tasks", "main"):
        return line
    gap = "\n" if line.startswith("\n") else ""
    body = line[len(gap):]
    if body.startswith("from ."):
        body = "from .." + body[len("from ."):]
        body = body.replace("\n" + " " * 15, "\n" + " " * 16) if "(" in body else body
    return gap + body


HEADERS = {
    "state": '"""Shared state of the control plane: the hub (AMI clients, live WebSocket feed, runtime\nregistry, locks) and the helpers more than one router or background task needs."""\n',
    "tasks": '"""Background tasks started by the application lifespan: card monitor, status poller, host\nhealth poller and media supervisor."""\n',
    "auth": '"""/api/auth/*: first-admin setup, login, logout and password change."""\n',
    "lines": '"""Lines and their SIM hardware: /api/instances, /api/provision, /api/cards, /api/readers,\n/api/sim/*, /api/ports/suggest and the reader device view /api/devices."""\n',
    "engine": '"""/api/engine/event: the callback each line\'s engine posts calls and registration events to."""\n',
    "softphone": '"""Browser softphone and call log: softphone config and WebSocket, call, hangup, calls,\n/api/media."""\n',
    "esim": '"""/api/esim/*: eUICC chip info, profiles, downloads and notifications through lpac."""\n',
    "egress": '"""/api/egress/*: country exit status, refresh and tests."""\n',
    "logs": '"""Line logs and the redacted support bundle."""\n',
    "system": '"""/api/settings, /api/system/* and the live event WebSocket /ws."""\n',
}


def generate(orig_src, moved):
    blocks, module_names, tail = split_an.segments(orig_src)
    src_lines = orig_src.splitlines(keepends=True)
    mod, by_name, imports = assign(blocks, moved)
    import_blocks = [b for b in blocks if mod[b.idx] == "<import>"]
    per = defaultdict(list)
    for b in blocks:
        m = mod[b.idx]
        if m == "<import>":
            continue
        text = rewrite_block(b, src_lines, by_name, mod, imports, module_names)
        if m == "main" and "app.add_middleware(gate.Gate)" in text:
            routers = [r for r in ROUTER_ORDER if r in set(mod.values())]
            text += "".join(f"app.include_router({import_ref(r)}.router)\n" for r in routers)
        per[m].append(text)
    files = {}
    for m, texts in per.items():
        # which other generated modules are referenced
        others = sorted(USED[m] - {m})
        if "main" in others:
            raise SystemExit(f"{m} references main")
        if m == "main":
            others = sorted(set(others) | {x for x in set(mod.values()) if x not in ("main", "<import>", "state", "tasks")})
        texts = [t if not t.startswith("\n") else t for t in texts]
        need_log = m != "main" and any(re.search(r"\blog\.", t) for t in texts)
        name_texts = texts + (["logging"] if need_log else [])
        imp, _ = used_imports(name_texts, import_blocks, src_lines)
        if m == "main":
            body = "".join(texts) + tail
            # main keeps its own docstring + future import in place
            head_end = body.index('"""', 3) + 3
            doc, rest = body[:head_end], body[head_end:]
            gen_imports = [x for x in imp]
            routers = [r for r in others if r not in ("state", "tasks")]
            local = [x for x in others if x in ("state", "tasks")]
            extra = ""
            if local:
                extra += "".join(f"from . import {x} as app_{x}\n" for x in local)
            if routers:
                extra += "".join(f"from .routers import {r} as {r}_routes\n" for r in routers)
            content = doc + "\nfrom __future__ import annotations\n\n" + "".join(gen_imports) + extra + rest.lstrip("\n").join(["\n\n", ""]) if False else None
            content = (doc + "\nfrom __future__ import annotations\n\n" + "".join(gen_imports)
                       + extra + "\n" + rest.lstrip("\n"))
        else:
            if m not in ("state", "tasks"):
                imp, _ = used_imports(name_texts, import_blocks, src_lines, {"fastapi": ["APIRouter"]})
            imp = [relative(m, x) for x in imp]
            imp = group_local(imp)
            extra = ""
            local = [x for x in others if x in ("state", "tasks")]
            routers = [x for x in others if x not in ("state", "tasks")]
            pkg = "." if m in ("state", "tasks") else ".."
            if local:
                extra += "".join(f"from {pkg} import {x} as app_{x}\n" for x in local)
            if routers:
                extra += "".join(f"from {pkg}{'routers' if pkg == '.' else ''} import {r} as {r}_routes\n" for r in routers)
            is_router = m not in ("state", "tasks")
            body = "".join(texts).lstrip("\n")
            pre = ""
            if any(re.search(r"\blog\.", t) for t in texts) and "log" not in {d for b in blocks if mod[b.idx] == m for d in b.defines}:
                pre += 'log = logging.getLogger("vowifi.main")\n'
                assert any("import logging" in x for x in imp)
            if is_router:
                pre += "router = APIRouter()\n"
            content = (HEADERS[m] + "from __future__ import annotations\n\n" + "".join(imp) + extra
                       + ("\n" + pre if pre else "") + "\n\n" + body)
        files[m] = content
    return files, mod, blocks


def main():
    orig = Path(sys.argv[1]).read_text()
    moved = set(sys.argv[2].split(",")) if sys.argv[2] not in ("", "none") else set()
    if "all" in moved:
        moved = {"state", "tasks", "auth", "lines", "engine", "softphone", "esim", "egress", "logs", "system"}
    files, mod, blocks = generate(orig, moved)
    (APP / "routers").mkdir(exist_ok=True)
    if any(m not in ("main", "state", "tasks") for m in files):
        init = APP / "routers" / "__init__.py"
        if not init.exists():
            init.write_text('"""API routers of the control plane, one module per area."""\n')
    for m, content in files.items():
        module_path(m).write_text(content)
    import json
    names = {}
    for b in blocks:
        for d in b.defines:
            names[d] = mod[b.idx]
    imports = {}
    for b in blocks:
        if mod[b.idx] == "<import>":
            n = b.node
            for a in n.names:
                bound = (a.asname or a.name).split(".")[0]
                if isinstance(n, ast.ImportFrom):
                    imports[bound] = {"from": "." * n.level + (n.module or ""), "name": a.name}
                else:
                    imports[bound] = {"import": a.name}
    Path(__file__).with_name("layout.json").write_text(json.dumps({"names": names, "imports": imports}, indent=1))
    counts = defaultdict(int)
    for b in blocks:
        counts[mod[b.idx]] += b.end - b.start + 1
    for m in sorted(counts):
        print(m, counts[m], "lines")
    if "--list" in sys.argv:
        for b in blocks:
            if mod[b.idx] not in ("main", "<import>"):
                print("  ", mod[b.idx], b.name)


if __name__ == "__main__":
    main()
