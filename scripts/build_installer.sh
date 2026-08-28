#!/usr/bin/env bash
# 制作 macOS PKG 向导安装包
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP_NAME="yoya.app"
PKG_ID="com.shinanwu.yoya"
PKG_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
COMPONENT_PKG="$DIST/yoya-component.pkg"
PRODUCT_PKG="$DIST/yoya.pkg"
UNSIGNED_PRODUCT_PKG="$DIST/yoya-unsigned.pkg"
COMPONENT_PLIST="$ROOT/packaging/component.plist"
PACKAGE_SCRIPTS="$ROOT/packaging/scripts"

APP_BUNDLE="$DIST/$APP_NAME"
if [ ! -d "$APP_BUNDLE" ]; then
  echo "错误: 未找到 dist/$APP_NAME，请先运行 PyInstaller 打包" >&2
  exit 1
fi

# 避免上一次构建的产物被 productbuild/productsign 误用或拒绝覆盖。
rm -f "$COMPONENT_PKG" "$UNSIGNED_PRODUCT_PKG" "$PRODUCT_PKG"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP_BUNDLE" "$STAGING/"
sed "s/__VERSION__/$PKG_VERSION/g" \
  "$ROOT/packaging/distribution.xml" > "$STAGING/distribution.xml"

echo "==> 制作组件 PKG（固定安装到 /Applications）"
pkgbuild \
  --root "$STAGING" \
  --component-plist "$COMPONENT_PLIST" \
  --scripts "$PACKAGE_SCRIPTS" \
  --install-location /Applications \
  --identifier "$PKG_ID" \
  --version "$PKG_VERSION" \
  "$COMPONENT_PKG"

echo "==> 制作向导安装包 PKG"
productbuild \
  --distribution "$STAGING/distribution.xml" \
  --resources "$ROOT/packaging/installer-resources" \
  --package-path "$DIST" \
  "$UNSIGNED_PRODUCT_PKG"

if [[ -n "${INSTALLER_SIGN_IDENTITY:-}" ]]; then
  echo "==> 使用 \$INSTALLER_SIGN_IDENTITY 签名安装包"
  productsign \
    --sign "$INSTALLER_SIGN_IDENTITY" \
    "$UNSIGNED_PRODUCT_PKG" \
    "$PRODUCT_PKG"
  rm -f "$UNSIGNED_PRODUCT_PKG"
else
  echo "==> 跳过安装包签名（未设置 INSTALLER_SIGN_IDENTITY）"
  mv "$UNSIGNED_PRODUCT_PKG" "$PRODUCT_PKG"
fi

rm -f "$COMPONENT_PKG"
rm -rf "$DIST/yoya" "$DIST/$APP_NAME"

echo ""
echo "完成："
echo "  安装包: $PRODUCT_PKG"
