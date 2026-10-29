#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

BUNDLE_NAME="autocad_mcp_bridge"
BUNDLE_DIR="$SCRIPT_DIR/${BUNDLE_NAME}.bundle"
CRX_DIR="$SCRIPT_DIR/${BUNDLE_NAME}.crx"

rm -rf "$BUNDLE_DIR" "$CRX_DIR"

# 1. Build .bundle
mkdir -p "$BUNDLE_DIR/Contents/MacOS"
mkdir -p "$BUNDLE_DIR/Contents/Resources"

cat > "$BUNDLE_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleExecutable</key>
	<string>${BUNDLE_NAME}</string>
	<key>CFBundleIdentifier</key>
	<string>com.antigravity.${BUNDLE_NAME}</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>${BUNDLE_NAME}</string>
	<key>CFBundlePackageType</key>
	<string>BNDL</string>
	<key>CFBundleShortVersionString</key>
	<string>26.0.60.171</string>
	<key>CFBundleSignature</key>
	<string>????</string>
	<key>CFBundleSupportedPlatforms</key>
	<array>
		<string>MacOSX</string>
	</array>
	<key>CFBundleVersion</key>
	<string>26.0.60.171</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
</dict>
</plist>
EOF

cp "$BUNDLE_DIR/Contents/Info.plist" "$BUNDLE_DIR/Contents/Resources/arx.plist"

clang++ -std=c++17 -O2 -fobjc-arc \
    -arch arm64 -arch x86_64 \
    -bundle \
    -framework Foundation -framework Cocoa \
    -Wl,-undefined,dynamic_lookup \
    "$SCRIPT_DIR/autocad_mcp_bridge.mm" \
    -o "$BUNDLE_DIR/Contents/MacOS/${BUNDLE_NAME}"

codesign --force --sign - "$BUNDLE_DIR"

# 2. Build .crx (Framework layout used by libaccore / arxload)
mkdir -p "$CRX_DIR/Versions/A/Resources/Base.lproj"
mkdir -p "$CRX_DIR/Versions/A/Resources/zh_CN.lproj"
touch "$CRX_DIR/Versions/A/Resources/Base.lproj/arx.strings"
touch "$CRX_DIR/Versions/A/Resources/zh_CN.lproj/arx.strings"

cat > "$CRX_DIR/Versions/A/Resources/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleExecutable</key>
	<string>${BUNDLE_NAME}</string>
	<key>CFBundleIdentifier</key>
	<string>com.autodesk.${BUNDLE_NAME}.crx</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>${BUNDLE_NAME}</string>
	<key>CFBundlePackageType</key>
	<string>FMWK</string>
	<key>CFBundleShortVersionString</key>
	<string>26.0.60.171</string>
	<key>CFBundleSignature</key>
	<string>????</string>
	<key>CFBundleSupportedPlatforms</key>
	<array>
		<string>MacOSX</string>
	</array>
	<key>CFBundleVersion</key>
	<string>26.0.60.171</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
</dict>
</plist>
EOF

cat > "$CRX_DIR/Versions/A/Resources/arx.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>AutoCADMCPBridge</key>
	<dict>
		<key>DESCRIPTION</key>
		<string>AutoCAD 2027 MCP Native Bridge</string>
		<key>Groups</key>
		<array/>
		<key>LOADCTRLS</key>
		<integer>13</integer>
		<key>appname</key>
		<string>AutoCADMCPBridge</string>
	</dict>
</dict>
</plist>
EOF

clang++ -std=c++17 -O2 -fobjc-arc \
    -arch arm64 -arch x86_64 \
    -dynamiclib \
    -install_name "@rpath/${BUNDLE_NAME}.crx/Versions/A/${BUNDLE_NAME}" \
    -framework Foundation -framework Cocoa \
    -Wl,-undefined,dynamic_lookup \
    "$SCRIPT_DIR/autocad_mcp_bridge.mm" \
    -o "$CRX_DIR/Versions/A/${BUNDLE_NAME}"

ln -s A "$CRX_DIR/Versions/Current"
ln -s "Versions/Current/${BUNDLE_NAME}" "$CRX_DIR/${BUNDLE_NAME}"
ln -s "Versions/Current/Resources" "$CRX_DIR/Resources"

codesign --force --sign - "$CRX_DIR/Versions/A/${BUNDLE_NAME}"
codesign --force --sign - "$CRX_DIR"

echo "Successfully built and signed:"
echo "  - $BUNDLE_DIR"
echo "  - $CRX_DIR"
