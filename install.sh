#!/bin/bash
# 使用国内镜像安装，避免 files.pythonhosted.org 慢速/超时
set -euo pipefail
cd "$(dirname "$0")"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

python3 -m venv .venv
.venv/bin/pip install -i "$MIRROR" --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
echo "安装完成。运行: source .venv/bin/activate && python run.py"
