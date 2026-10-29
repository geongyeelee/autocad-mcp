#!/bin/zsh
set -euo pipefail

REPO_DIR="${0:A:h}"
cd "$REPO_DIR"

echo "==> [1/4] Building native AutoCAD 2027 bridge (.crx & .bundle)..."
chmod +x "$REPO_DIR/bridge/build_bridge.sh"
"$REPO_DIR/bridge/build_bridge.sh"

echo "==> [2/4] Installing acaddoc.lsp auto-loader into AutoCAD 2027 user Support directories..."
ACAD_ROAMING="$HOME/Library/Application Support/Autodesk/AutoCAD 2027/R26.0/roaming"
if [[ -d "$ACAD_ROAMING" ]]; then
  for support_dir in "$ACAD_ROAMING"/@*@/Support(N); do
    cat > "$support_dir/acaddoc.lsp" <<EOF
;;; AutoCAD 2027 for Mac - MCP Bridge Document Auto-Loader (acaddoc.lsp)
(vl-catch-all-apply
  '(lambda ()
     (if (/= (getvar "SECURELOAD") 0)
       (setvar "SECURELOAD" 0)
     )
     (load "$REPO_DIR/bridge/mcp_helpers.lsp")
     (if (and (or (member "acapp.crx" (arx)) (member "AcApp.crx" (arx)))
              (not (member "autocad_mcp_bridge.crx" (arx))))
       (arxload "$REPO_DIR/bridge/autocad_mcp_bridge.crx")
     )
   )
  nil
)
(princ)
EOF
    echo "    Installed: $support_dir/acaddoc.lsp"
  done
else
  echo "    Warning: AutoCAD 2027 roaming folder not found at $ACAD_ROAMING"
fi

echo "==> [3/4] Setting up Python virtual environment (.venv)..."
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"

echo "==> [4/4] Installation complete!"
echo ""
echo "Add to Codex (~/.codex/config.toml):"
echo "[mcp_servers.autocad]"
echo "command = \"$REPO_DIR/.venv/bin/autocad-mcp\""
