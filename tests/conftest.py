import sys
from pathlib import Path

# 支持在 AstrBot 插件目录内独立运行测试。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
