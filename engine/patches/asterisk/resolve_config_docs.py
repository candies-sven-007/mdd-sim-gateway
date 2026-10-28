"""Keep sysmocom's private PJSIP resolve fields out of Asterisk XML docs.

The resolve sorcery type is a sysmocom extension and has no matching configObject in
pjsip_config.xml.  Registering its fields through the documenting API therefore emits a false
startup ERROR for the ``transport`` option.  The nodoc API has identical runtime configuration
semantics and is the intended interface for fields without XML documentation.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip/config_resolve.c"


def patch(source: str) -> str:
    marker = "int ast_sip_initialize_sorcery_resolve(void)"
    start = source.find(marker)
    if start < 0:
        raise ValueError("resolve initializer not found")
    body_start = source.find("{", start)
    depth = 0
    end = -1
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        raise ValueError("resolve initializer brace match failed")

    body = source[start:end]
    if "ast_sorcery_object_field_register_nodoc" in body:
        return source
    registrations = body.count("ast_sorcery_object_field_register(")
    if registrations != 3:
        raise ValueError(f"expected 3 resolve field registrations, found {registrations}")
    body = body.replace(
        "ast_sorcery_object_field_register(",
        "ast_sorcery_object_field_register_nodoc(",
    )
    # The fork accidentally left two raw configure-time debug markers in this initializer.
    body = body.replace('\n\tputs("a");', "").replace('\n\tputs("b");', "")
    return source[:start] + body + source[end:]


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"resolve config patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

SOURCE.write_text(updated)
print("patched PJSIP resolve fields to use nodoc registration")
