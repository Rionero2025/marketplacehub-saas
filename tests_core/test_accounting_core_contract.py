from __future__ import annotations

import ast
from pathlib import Path


def test_core_has_no_streamlit_imports():
    root = Path(__file__).resolve().parents[1] / "marketplace_core"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "streamlit" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "streamlit"


def test_accounting_period_rejects_reverse_dates():
    from datetime import date
    from marketplace_core.accounting import AccountingPeriod
    try:
        AccountingPeriod(date(2026, 9, 2), date(2026, 9, 1))
    except ValueError:
        return
    raise AssertionError("AccountingPeriod accepted an invalid range")
