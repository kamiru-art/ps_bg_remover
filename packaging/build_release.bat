@echo off
setlocal
REM ============================================================
REM  Kamiru - build del ejecutable autocontenido (Windows)
REM  Produce dist\Kamiru\Kamiru.exe y Kamiru-win64-cuda.zip o
REM  Kamiru-win64-cpu.zip, segun la variante de PyTorch.
REM  Con GPU NVIDIA presente empaqueta PyTorch CUDA 12.8 (el
REM  build GPU pesa varios GB: distribuirlo por USB/red local).
REM ============================================================
cd /d "%~dp0.."

REM --- uv para instalar rapido (se bootstrapea con pip si falta)
where uv >nul 2>&1
if %errorlevel%==0 (
    set "UV=uv"
) else (
    py -3.12 -m pip install -q uv || python -m pip install -q uv
    set "UV=py -3.12 -m uv"
)

%UV% venv build-env --python 3.12 || (echo ERROR creando build-env & exit /b 1)
set "PY=build-env\Scripts\python.exe"

REM --reinstall-package: build-env sobrevive entre builds y puede traer la
REM otra variante de torch de un build anterior
where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
    echo Empaquetando con PyTorch CUDA 12.8...
    set "ZIP=Kamiru-win64-cuda.zip"
    %UV% pip install --python %PY% --reinstall-package torch --reinstall-package torchvision torch torchvision --index-url https://download.pytorch.org/whl/cu128 || (echo ERROR instalando PyTorch CUDA & exit /b 1)
) else (
    echo Empaquetando con PyTorch CPU...
    set "ZIP=Kamiru-win64-cpu.zip"
    %UV% pip install --python %PY% --reinstall-package torch --reinstall-package torchvision torch torchvision || (echo ERROR instalando PyTorch CPU & exit /b 1)
)
%UV% pip install --python %PY% -e . pyinstaller || (echo ERROR instalando la app & exit /b 1)

%PY% -m PyInstaller packaging\kamiru.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 (echo ERROR en PyInstaller & exit /b 1)

echo Comprimiendo dist\Kamiru...
powershell -NoLogo -Command "Compress-Archive -Force -Path dist\Kamiru -DestinationPath %ZIP%"
echo.
echo Listo: dist\Kamiru\Kamiru.exe  y  %ZIP%
echo Al primer uso la app descarga el modelo a la carpeta models\ junto al exe.
pause
