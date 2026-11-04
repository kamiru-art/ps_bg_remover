@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Kamiru - instalacion en Windows (correr UNA vez, Sebastian)
REM  Usa uv (instalador rapido). Crea el venv, instala PyTorch
REM  CUDA para la RTX 5070 Ti, la app y descarga el modelo.
REM ============================================================
cd /d "%~dp0"

REM Kamiru.bat abre la app solo si existe este marcador. Se borra al empezar
REM y se crea en el paso 6, cuando la app ya puede abrir.
set "MARCA=venv\kamiru_instalado.txt"
if exist "%MARCA%" del "%MARCA%"

echo.
echo === Kamiru: instalacion ===
echo.

REM --- 1. Encontrar Python 3.10-3.12 (RMBG-2.0 no soporta 3.13)
set "PYCMD="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYCMD (
        py -%%V -c "print()" >nul 2>&1 && set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info < (3,13) else 1)" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo ERROR: No se encontro Python 3.10, 3.11 o 3.12.
    echo Instala Python 3.12 desde https://www.python.org/downloads/
    echo IMPORTANTE: marcar "Add Python to PATH" al instalar.
    pause
    exit /b 1
)
echo Usando: %PYCMD%

REM --- 2. uv: instalador rapido de paquetes (se bootstrapea con pip)
where uv >nul 2>&1
if %errorlevel%==0 (
    set "UV=uv"
) else (
    echo Instalando uv...
    %PYCMD% -m pip install -q uv || (echo ERROR instalando uv & pause & exit /b 1)
    set "UV=%PYCMD% -m uv"
)

REM --- 3. Crear el entorno virtual con uv
if not exist venv (
    %UV% venv venv || (echo ERROR creando venv & pause & exit /b 1)
)
set "VPY=venv\Scripts\python.exe"

REM --- 4. PyTorch: CUDA 12.8 si hay GPU NVIDIA (5070 Ti = Blackwell), CPU si no
where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
    echo Instalando PyTorch con CUDA 12.8 ^(GPU NVIDIA detectada^)...
    REM --reinstall-package: si quedo un torch CPU de antes, reemplazarlo si o si
    %UV% pip install --python %VPY% --reinstall-package torch --reinstall-package torchvision torch torchvision --index-url https://download.pytorch.org/whl/cu128 || (
        echo ERROR instalando PyTorch CUDA & pause & exit /b 1
    )
) else (
    echo AVISO: no se detecto GPU NVIDIA. Instalando PyTorch para CPU ^(mas lento^).
    %UV% pip install --python %VPY% torch torchvision || (
        echo ERROR instalando PyTorch & pause & exit /b 1
    )
)

REM --- 5. Instalar la app y sus dependencias
%UV% pip install --python %VPY% -e . || (echo ERROR instalando dependencias & pause & exit /b 1)

REM --- 6. Verificar la GPU y que la app pueda abrir
%VPY% -c "import torch; ok=torch.cuda.is_available(); print('CUDA disponible:', ok); print('GPU:', torch.cuda.get_device_name(0) if ok else 'ninguna (se usara CPU, mas lento)')"
%VPY% -c "import torch, kamiru.gui.app" || (echo ERROR: la app no quedo instalada. Revisa los mensajes de arriba. & pause & exit /b 1)
echo ok> "%MARCA%"

REM --- 7. Token de HuggingFace (RMBG-2.0 es un repo con registro)
if not exist models mkdir models
if not exist models\hf_token.txt (
    echo.
    echo RMBG-2.0 requiere una cuenta GRATIS de HuggingFace:
    echo   1. Crear cuenta:      https://huggingface.co/join
    echo   2. Aceptar licencia:  https://huggingface.co/briaai/RMBG-2.0
    echo   3. Crear token Read:  https://huggingface.co/settings/tokens
    echo.
    set /p HFTOK="Pega aqui el token (o Enter para usar el modelo alternativo BiRefNet): "
    if defined HFTOK (echo !HFTOK!)> models\hf_token.txt
)

REM --- 8. Descargar y cachear el modelo (una sola vez)
echo.
echo Descargando el modelo (~1 GB, solo esta vez)...
%VPY% scripts\download_model.py rmbg-2.0 || (
    echo ERROR descargando el modelo. Revisa la conexion y reintenta.
    pause
    exit /b 1
)

echo.
echo === Listo. Para usar la app: doble clic en Kamiru.bat ===
echo (Puedes crear un acceso directo de Kamiru.bat en el escritorio de Kamila)
pause
