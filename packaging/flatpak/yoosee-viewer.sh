#!/bin/sh
# Launcher do Flatpak: o app em si é Python puro, instalado em /app/share/yoosee-viewer.
exec python3 /app/share/yoosee-viewer/app.py "$@"
