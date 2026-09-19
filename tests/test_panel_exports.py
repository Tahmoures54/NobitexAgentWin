import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _class_names(relative: str) -> list[str]:
    src = (_ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef)]


def test_paper_panel_exports_paper_trading_panel():
    names = _class_names("gui/panels/paper_trading_panel.py")
    assert "PaperTradingPanel" in names
    assert "RealTradingPanel" not in names


def test_real_panel_exports_real_trading_panel():
    names = _class_names("gui/panels/real_trading_panel.py")
    assert "RealTradingPanel" in names
