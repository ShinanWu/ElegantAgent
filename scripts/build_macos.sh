#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/public/vendor"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
DIST="$ROOT/dist"
APP_NAME="yoya.app"

cd "$ROOT"

echo "==> 下载前端 vendor 资源"
mkdir -p "$VENDOR"
curl -fsSL -o "$VENDOR/marked.min.js" \
  "https://unpkg.com/marked@12.0.2/marked.min.js"
curl -fsSL -o "$VENDOR/highlight.min.js" \
  "https://unpkg.com/@highlightjs/cdn-assets@11.9.0/highlight.min.js"
curl -fsSL -o "$VENDOR/highlight.github-dark.min.css" \
  "https://unpkg.com/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css"

echo "==> 准备 Python 环境"
python3 -m venv .venv
source .venv/bin/activate
pip install -q -i "$MIRROR" --trusted-host pypi.tuna.tsinghua.edu.cn \
  -r requirements.txt -r requirements-build.txt

echo "==> 生成应用图标"
python3 packaging/make_icon.py

echo "==> PyInstaller 打包"
pyinstaller --noconfirm --clean packaging/yoya.spec

APP_BUNDLE="$DIST/$APP_NAME"
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> 使用 \$CODESIGN_IDENTITY 签名"
  codesign --deep --force --options runtime --timestamp \
    --sign "$CODESIGN_IDENTITY" "$APP_BUNDLE"
else
  echo "==> 跳过 codesign（未设置 CODESIGN_IDENTITY）"
fi

echo "==> 制作 PKG 安装包"
bash scripts/build_installer.sh

if [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
  echo "==> 公证安装包"
  xcrun notarytool submit "$DIST/yoya.pkg" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --wait
  xcrun stapler staple "$DIST/yoya.pkg"
else
  echo "==> 跳过公证（未设置 APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD）"
fi

echo ""
echo "完成："
echo "  安装包: dist/yoya.pkg"
echo ""
echo "用户双击 .pkg，按「继续」完成安装。"
