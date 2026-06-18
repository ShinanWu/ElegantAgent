#!/usr/bin/env bash
# 制作 macOS PKG 向导安装包
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP_NAME="Cursor Agent π.app"
PKG_ID="com.cursor.agent.pi"
PKG_VERSION="1.0.1"
COMPONENT_PKG="$DIST/CursorAgentPi-component.pkg"
PRODUCT_PKG="$DIST/CursorAgentPi.pkg"
COMPONENT_PLIST="$ROOT/packaging/component.plist"

APP_BUNDLE="$DIST/$APP_NAME"
if [ ! -d "$APP_BUNDLE" ]; then
  echo "错误: 未找到 dist/$APP_NAME，请先运行 PyInstaller 打包" >&2
  exit 1
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP_BUNDLE" "$STAGING/"

echo "==> 制作组件 PKG（固定安装到 /Applications）"
pkgbuild \
  --root "$STAGING" \
  --component-plist "$COMPONENT_PLIST" \
  --install-location /Applications \
  --identifier "$PKG_ID" \
  --version "$PKG_VERSION" \
  "$COMPONENT_PKG"

echo "==> 制作向导安装包 PKG"
productbuild \
  --distribution "$ROOT/packaging/distribution.xml" \
  --resources "$ROOT/packaging/installer-resources" \
  --package-path "$DIST" \
  "$PRODUCT_PKG"

rm -f "$COMPONENT_PKG"
rm -rf "$DIST/CursorAgentPi" "$DIST/$APP_NAME"

echo ""
echo "完成："
echo "  安装包: $PRODUCT_PKG"
