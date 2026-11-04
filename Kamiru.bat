@echo off
REM Kamiru - lanzador de doble clic (Windows)
cd /d "%~dp0"
REM setup_windows.bat crea el marcador solo cuando la app ya puede abrir
if not exist venv\kamiru_instalado.txt (
    echo Falta terminar la instalacion. Corriendo setup_windows.bat...
    call setup_windows.bat
    if not exist venv\kamiru_instalado.txt exit /b 1
)
start "" venv\Scripts\pythonw.exe -m kamiru.gui.app
