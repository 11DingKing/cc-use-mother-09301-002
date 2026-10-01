"""测试包：统一注入 src 到 sys.path，保证各测试模块可直接导入后端。"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
