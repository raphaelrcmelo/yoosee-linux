#!/usr/bin/env bash
# Remove o Câmeras Yoosee. Use --purge para apagar também contas e câmeras salvas.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"

rm -rf "$DATA/yoosee-viewer"
rm -f "$HOME/.local/bin/yoosee-viewer" \
      "$DATA/applications/yoosee-viewer.desktop" \
      "$DATA/icons/hicolor/scalable/apps/yoosee-viewer.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true

if [ "${1:-}" = "--purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/yoosee-viewer"
    echo "Removido, incluindo contas e câmeras salvas."
else
    echo "Removido. Suas contas e câmeras ficaram em ~/.config/yoosee-viewer (apague com --purge)."
fi
