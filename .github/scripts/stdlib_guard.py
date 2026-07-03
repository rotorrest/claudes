#!/usr/bin/env python3
"""Guardia de supply chain: falla si algún script importa algo fuera de la
stdlib. claudes promete cero dependencias; este check lo vuelve verificable
en CI en vez de una promesa del README."""

import ast
import sys


def main():
    allowed = sys.stdlib_module_names
    bad = []
    for path in sys.argv[1:]:
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                root = m.split(".")[0]
                if root and root not in allowed:
                    bad.append(f"{path}:{node.lineno} importa '{root}' (no-stdlib)")
    if bad:
        for b in bad:
            print(f"::error::{b}")
        sys.exit(1)
    print(f"stdlib-only OK ({len(sys.argv) - 1} archivos)")


if __name__ == "__main__":
    main()
