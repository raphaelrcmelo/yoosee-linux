#!/usr/bin/env bash
# Instalador do Câmeras Yoosee (cliente não-oficial para Linux).
#
#   ./install.sh                     instala o app (câmeras da rede local)
#   ./install.sh --cloud [Yoosee.exe] também prepara câmeras fora da rede (pela nuvem)
#
# Tudo vai para a sua pasta pessoal; não usa sudo.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
RUNTIME="$DATA/yoosee-viewer"
APP="$RUNTIME/app"
BIN="$HOME/.local/bin"
PY_EMBED_URL="https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PY_EMBED_SHA256="4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
SDK_FILES=(IotVideoSDK.dll msvcp140.dll vcruntime140.dll vcruntime140_1.dll)

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m !\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

hint_pkg() {  # comando de instalação conforme a distro
    local deb="$1" fed="$2" arch="$3"
    if command -v apt-get >/dev/null; then echo "sudo apt install $deb"
    elif command -v dnf >/dev/null; then echo "sudo dnf install $fed"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S $arch"
    elif command -v zypper >/dev/null; then echo "sudo zypper install $fed"
    else echo "instale: $deb"; fi
}

# ---------------------------------------------------------------- app base
install_app() {
    say "Verificando dependências"
    command -v python3 >/dev/null || die "python3 não encontrado. $(hint_pkg python3 python3 python)"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
        || die "Precisa de Python 3.10 ou mais novo (você tem $(python3 -V))."
    python3 -c 'import venv, ensurepip' 2>/dev/null \
        || die "Módulo venv do Python ausente. $(hint_pkg python3-venv python3 python)"
    ok "$(python3 -V)"

    say "Copiando o programa para $APP"
    mkdir -p "$APP"
    for f in app.py store.py settings.py p2p.py lan.py ptz.py yoosee_api.py requirements.txt; do
        install -m 644 "$SRC/$f" "$APP/$f"
    done
    mkdir -p "$APP/winsdk"
    install -m 644 "$SRC/winsdk/bridge.py" "$APP/winsdk/bridge.py"
    ok "arquivos copiados"

    say "Instalando bibliotecas Python (PySide6 ~ 200 MB, pode demorar)"
    [ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
    "$APP/.venv/bin/python" -m pip install --quiet --upgrade pip
    "$APP/.venv/bin/python" -m pip install --quiet -r "$APP/requirements.txt"
    ok "bibliotecas instaladas"

    say "Criando atalho no menu"
    mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
    cat > "$BIN/yoosee-viewer" <<EOF
#!/bin/sh
exec "$APP/.venv/bin/python" "$APP/app.py" "\$@"
EOF
    chmod 755 "$BIN/yoosee-viewer"
    install -m 644 "$SRC/packaging/yoosee-viewer.svg" "$DATA/icons/hicolor/scalable/apps/yoosee-viewer.svg"
    cat > "$DATA/applications/yoosee-viewer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Câmeras Yoosee
Comment=Visualizador de câmeras Yoosee com PTZ e múltiplas contas
Exec=$BIN/yoosee-viewer
Icon=yoosee-viewer
Terminal=false
Categories=AudioVideo;Video;
EOF
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
    ok "\"Câmeras Yoosee\" no menu de aplicativos (ou rode: yoosee-viewer)"
}

# ------------------------------------------------- câmeras fora da rede local
find_wine() {
    if [ -n "${YOOSEE_WINE:-}" ]; then [ -x "$YOOSEE_WINE" ] && echo "$YOOSEE_WINE"; return; fi
    local w r
    for w in wine64 wine; do command -v "$w" >/dev/null && { command -v "$w"; return; }; done
    for r in "$HOME"/.var/app/com.usebottles.bottles/data/bottles/runners/* "$DATA"/bottles/runners/*; do
        for w in wine64 wine; do [ -x "$r/bin/$w" ] && { echo "$r/bin/$w"; return; }; done
    done
    return 1
}

wine_run() {  # mesmo ambiente que o p2p.py usa
    local runner; runner="$(dirname "$(dirname "$WINE")")"
    local libs=""
    [ -d "$runner/lib/wine" ] && [ "$runner" != "/usr" ] && \
        libs="$runner/lib:$runner/lib/wine/x86_64-unix:$runner/lib32/wine/i386-unix"
    env WINEARCH=win64 WINEPREFIX="$RUNTIME/wineprefix" WINEDEBUG=-all \
        WINEDLLOVERRIDES="winemenubuilder=;mscoree=;mshtml=" \
        ${libs:+LD_LIBRARY_PATH="$libs"} "$WINE" "$@"
}

copy_sdk_from() {  # $1 = pasta onde o Yoosee oficial está instalado
    local f
    for f in "${SDK_FILES[@]}"; do [ -f "$1/$f" ] || return 1; done
    mkdir -p "$RUNTIME/sdk"
    for f in "${SDK_FILES[@]}"; do install -m 644 "$1/$f" "$RUNTIME/sdk/$f"; done
}

get_sdk() {
    local installer="${1:-}"
    if [ -f "$RUNTIME/sdk/IotVideoSDK.dll" ]; then ok "SDK já presente"; return; fi

    # 1) Yoosee oficial já instalado em algum prefixo Wine/Bottles
    local dll
    while IFS= read -r dll; do
        if copy_sdk_from "$(dirname "$dll")"; then ok "SDK copiado de $(dirname "$dll")"; return; fi
    done < <(find -L "$HOME/.wine" "$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles" \
                  "$DATA/bottles/bottles" "$HOME/Games" "$HOME/Downloads" \
                  -maxdepth 6 -iname IotVideoSDK.dll -path '*Yoosee*' 2>/dev/null)

    # 2) a partir do instalador oficial (Yoosee-*.exe)
    if [ -z "$installer" ]; then
        installer="$(ls -t "$HOME"/Downloads/Yoosee*.exe 2>/dev/null | head -1 || true)"
    fi
    [ -n "$installer" ] && [ -f "$installer" ] || die "Não achei o Yoosee oficial.
   Baixe o instalador para Windows em https://www.yoosee.co (Yoosee-x.y.z.exe) e rode:
     ./install.sh --cloud ~/Downloads/Yoosee-1.0.0.8.exe"

    local tmp; tmp="$(mktemp -d)"
    if command -v 7z >/dev/null; then
        say "Extraindo o SDK do instalador com 7z"
        7z e -y -o"$tmp" "$installer" "${SDK_FILES[@]/#/-ir!}" >/dev/null
        copy_sdk_from "$tmp" || { rm -rf "$tmp"; die "O instalador não contém ${SDK_FILES[*]}."; }
    else
        say "Rodando o instalador oficial em modo silencioso no Wine (só para pegar o SDK)"
        wine_run "$installer" /S '/D=C:\YooseeOficial' >/dev/null 2>&1 || true
        copy_sdk_from "$RUNTIME/wineprefix/drive_c/YooseeOficial" \
            || { rm -rf "$tmp"; die "Falhou. Instale o 7z ($(hint_pkg 7zip p7zip-plugins 7zip)) e tente de novo."; }
    fi
    rm -rf "$tmp"
    ok "SDK extraído do instalador oficial"
}

get_win_python() {
    if [ -f "$RUNTIME/winpython/python.exe" ]; then ok "Python para Windows já presente"; return; fi
    say "Baixando Python 3.12 para Windows (python.org, ~11 MB)"
    local tmp; tmp="$(mktemp -d)"
    if command -v curl >/dev/null; then curl -fsSL -o "$tmp/py.zip" "$PY_EMBED_URL"
    else wget -qO "$tmp/py.zip" "$PY_EMBED_URL"; fi
    echo "$PY_EMBED_SHA256  $tmp/py.zip" | sha256sum -c --quiet - || { rm -rf "$tmp"; die "Checksum do Python não confere."; }
    mkdir -p "$RUNTIME/winpython"
    python3 -m zipfile -e "$tmp/py.zip" "$RUNTIME/winpython"
    rm -rf "$tmp"
    ok "Python para Windows instalado"
}

install_cloud() {
    say "Preparando câmeras fora da rede local"
    WINE="$(find_wine)" || die "Wine não encontrado. $(hint_pkg wine wine wine)
   (ou instale o Bottles pelo Flathub, ou aponte YOOSEE_WINE=/caminho/do/wine)"
    ok "Wine: $WINE"
    if [ ! -f "$RUNTIME/wineprefix/system.reg" ]; then
        say "Criando prefixo Wine (primeira vez demora ~1 min)"
        mkdir -p "$RUNTIME/wineprefix"
        wine_run wineboot -i >/dev/null 2>&1 || true
        # o registro só é gravado quando o wineserver encerra
        for _ in $(seq 60); do [ -f "$RUNTIME/wineprefix/system.reg" ] && break; sleep 1; done
        [ -f "$RUNTIME/wineprefix/system.reg" ] || die "O Wine não conseguiu criar o prefixo ($WINE)."
    fi
    get_sdk "${1:-}"
    get_win_python
    ok "Câmeras pela nuvem prontas"
}

# --------------------------------------------------------------------- main
CLOUD=0; INSTALLER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --cloud) CLOUD=1; [ $# -gt 1 ] && [ "${2:0:2}" != "--" ] && { INSTALLER="$2"; shift; } ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) die "Opção desconhecida: $1" ;;
    esac
    shift
done

[ "$(uname -m)" = "x86_64" ] || warn "Testado só em x86_64; câmeras pela nuvem não funcionam em ARM."
install_app
if [ "$CLOUD" = 1 ]; then install_cloud "$INSTALLER"
else
    echo
    echo "Câmeras da rede local funcionam já. Para câmeras de outra casa / compartilhadas,"
    echo "rode depois:  ./install.sh --cloud"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN não está no PATH; use o atalho do menu." ;; esac
echo; ok "Pronto!"
