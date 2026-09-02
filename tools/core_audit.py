from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def analyze(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {"file": str(path.relative_to(ROOT)), "syntax_error": str(exc)}
    imports: list[str] = []
    functions = 0
    classes = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions += 1
        elif isinstance(node, ast.ClassDef):
            classes += 1
    return {
        "file": str(path.relative_to(ROOT)),
        "lines": text.count("\n") + 1,
        "functions": functions,
        "classes": classes,
        "imports_streamlit": any(x == "streamlit" or x.startswith("streamlit.") for x in imports),
    }


def main() -> None:
    files = sorted((ROOT / "pages").rglob("*.py")) + sorted((ROOT / "services").rglob("*.py"))
    report = [analyze(path) for path in files]
    summary = {
        "page_count": sum(item["file"].startswith("pages/") for item in report),
        "service_count": sum(item["file"].startswith("services/") for item in report),
        "service_streamlit_coupling": [
            item["file"] for item in report
            if item["file"].startswith("services/") and item.get("imports_streamlit")
        ],
        "largest_pages": sorted(
            [item for item in report if item["file"].startswith("pages/")],
            key=lambda item: item.get("lines", 0),
            reverse=True,
        )[:10],
    }
    output = {"summary": summary, "files": report}
    target = ROOT / "CORE_AUDIT.json"
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
