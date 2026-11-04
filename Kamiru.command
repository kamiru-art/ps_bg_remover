#!/bin/bash
# Kamiru - lanzador de doble clic (macOS)
cd "$(dirname "$0")"
# setup_macos.command crea el marcador solo cuando la app ya puede abrir
if [ ! -f venv/kamiru_instalado.txt ]; then
    echo "Falta terminar la instalación. Corriendo setup_macos.command..."
    bash setup_macos.command
    [ -f venv/kamiru_instalado.txt ] || exit 1
fi
exec ./venv/bin/python -m kamiru.gui.app
