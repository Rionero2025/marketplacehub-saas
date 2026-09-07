#!/usr/bin/env python3
"""Build a read-only structural manifest of the Marketplace Hub Streamlit source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


WIDGETS = {
    "button",
    "checkbox",
    "data_editor",
    "date_input",
    "download_button",
    "file_uploader",
    "form_submit_button",
    "multiselect",
    "number_input",
    "radio",
    "selectbox",
    "text_area",
    "text_input",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def expression(source: str, node: ast.AST | None) -> str:
    if node is None:
        return ""
    value = ast.get_source_segment(source, node) or ""
    return " ".join(value.split())[:500]


def inspect_python(path: Path, root: Path) -> dict:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    functions = []
    classes = []
    imports = []
    widgets = []
    tests = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item = {"name": node.name, "line": node.lineno}
            if node.name.startswith("test_"):
                tests.append(item)
            elif not node.name.startswith("_"):
                functions.append(item)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("services"):
                imports.append(
                    {
                        "module": module,
                        "names": sorted(alias.name for alias in node.names),
                        "line": node.lineno,
                    }
                )
        elif isinstance(node, ast.Call):
            call_name = dotted_name(node.func)
            short_name = call_name.rsplit(".", 1)[-1]
            if call_name.startswith("st.") and short_name in WIDGETS:
                widgets.append(
                    {
                        "kind": short_name,
                        "line": node.lineno,
                        "label_expression": expression(
                            source, node.args[0] if node.args else None
                        ),
                    }
                )

    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256(path),
        "line_count": len(source.splitlines()),
        "public_functions": sorted(functions, key=lambda item: item["line"]),
        "public_classes": sorted(classes, key=lambda item: item["line"]),
        "service_imports": sorted(imports, key=lambda item: item["line"]),
        "streamlit_interactions": sorted(widgets, key=lambda item: item["line"]),
        "test_cases": sorted(tests, key=lambda item: item["line"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-label", default="streamlit-reference")
    args = parser.parse_args()

    root = args.source.resolve()
    if not (root / "app.py").is_file():
        raise SystemExit(f"Streamlit source not found: {root}")

    paths = [root / "app.py"]
    for folder in ("pages", "services", "tests"):
        base = root / folder
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.py")))

    records = [inspect_python(path, root) for path in paths]
    manifest = {
        "source": args.source_label,
        "version": (root / "VERSION.txt").read_text(encoding="utf-8").strip(),
        "files": records,
        "totals": {
            "python_files": len(records),
            "lines": sum(item["line_count"] for item in records),
            "public_functions": sum(len(item["public_functions"]) for item in records),
            "public_classes": sum(len(item["public_classes"]) for item in records),
            "streamlit_interactions": sum(
                len(item["streamlit_interactions"]) for item in records
            ),
            "test_cases": sum(len(item["test_cases"]) for item in records),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
