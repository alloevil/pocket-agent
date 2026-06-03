"""运行全部测试：python tests/run_all.py

每个 test_*.py 既是独立脚本（python tests/test_x.py），也可由这里统一跑。
真实 agent 端到端测试会在对应 CLI 未安装时自动跳过。
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    files = sorted(HERE.glob("test_*.py"))
    failed = []
    for f in files:
        print(f"\n{'='*60}\n▶ {f.name}\n{'='*60}")
        rc = subprocess.run([sys.executable, str(f)]).returncode
        if rc != 0:
            failed.append(f.name)
    print(f"\n{'='*60}")
    if failed:
        print(f"❌ FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"✅ ALL {len(files)} test files passed")


if __name__ == "__main__":
    main()
