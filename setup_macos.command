#!/bin/bash
# ============================================================
#  Kamiru - instalación en macOS (correr UNA vez)
#  Usa uv (instalador rápido). Crea el venv, instala PyTorch
#  (GPU Apple vía MPS), la app y descarga el modelo.
# ============================================================
set -e
cd "$(dirname "$0")"

# Kamiru.command abre la app solo si existe este marcador. Se borra al
# empezar y se crea en el paso 6, cuando la app ya puede abrir.
MARCA=venv/kamiru_instalado.txt
rm -f "$MARCA"

echo
echo "=== Kamiru: instalación ==="
echo

# --- 1. Python 3.10-3.12 (RMBG-2.0 no soporta 3.13)
PYCMD=""
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info < (3,13) else 1)'; then
            PYCMD="$cand"; break
        fi
    fi
done
if [ -z "$PYCMD" ]; then
    echo "ERROR: no se encontró Python 3.10–3.12."
    echo "Instálalo con:  brew install python@3.12"
    echo "o desde https://www.python.org/downloads/"
    read -r -p "Enter para cerrar..."; exit 1
fi
echo "Usando: $PYCMD ($($PYCMD --version))"

# --- 2. uv: instalador rápido (bootstrap con pip si falta)
if command -v uv >/dev/null 2>&1; then
    UV=uv
else
    echo "Instalando uv..."
    "$PYCMD" -m pip install -q uv
    UV="$PYCMD -m uv"
fi

# --- 3. Entorno virtual
[ -d venv ] || $UV venv venv
VPY=venv/bin/python

# --- 4. PyTorch (los wheels de macOS traen soporte MPS para GPU Apple)
$UV pip install --python "$VPY" torch torchvision

# --- 5. La app y sus dependencias
$UV pip install --python "$VPY" -e .

# --- 6. Verificar la GPU Apple y que la app pueda abrir
"$VPY" -c "import torch; ok=torch.backends.mps.is_available(); print('GPU Apple (MPS):', 'disponible' if ok else 'no disponible (se usará CPU, más lento)')"
if ! "$VPY" -c "import torch, kamiru.gui.app"; then
    echo "ERROR: la app no quedó instalada. Revisa los mensajes de arriba."
    read -r -p "Enter para cerrar..."; exit 1
fi
echo ok > "$MARCA"

# --- 7. Token de HuggingFace (RMBG-2.0 es un repo con registro)
mkdir -p models
if [ ! -f models/hf_token.txt ]; then
    echo
    echo "RMBG-2.0 requiere una cuenta GRATIS de HuggingFace:"
    echo "  1. Crear cuenta:      https://huggingface.co/join"
    echo "  2. Aceptar licencia:  https://huggingface.co/briaai/RMBG-2.0"
    echo "  3. Crear token Read:  https://huggingface.co/settings/tokens"
    echo
    read -r -p "Pega aquí el token (o Enter para usar el modelo alternativo BiRefNet): " HFTOK
    [ -n "$HFTOK" ] && printf '%s\n' "$HFTOK" > models/hf_token.txt
fi

# --- 8. Descargar y cachear el modelo (una sola vez)
echo
echo "Descargando el modelo (~1 GB, solo esta vez)..."
"$VPY" scripts/download_model.py rmbg-2.0

echo
echo "=== Listo. Para usar la app: doble clic en Kamiru.command ==="
read -r -p "Enter para cerrar..."
