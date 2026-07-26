from __future__ import annotations

import compileall
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更高版本。")
    (ROOT / "data").mkdir(exist_ok=True)
    if not compileall.compile_dir(ROOT / "backend", quiet=1):
        raise SystemExit("后端语法检查失败。")
    sys.path.insert(0, str(ROOT / "backend"))
    from db import init_db
    init_db()
    print("安装检查完成：无需 pip、npm 或外部依赖。")
    print("运行 start_windows.cmd 或 start_unix.sh 启动。")


if __name__ == "__main__":
    main()
