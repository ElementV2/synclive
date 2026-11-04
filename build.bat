@echo off
cd /d "%~dp0"
REM Construit dist\SyncLive\SyncLive.exe (dossier, pas un exe unique : les DLL Qt (LGPL) restent remplacables)
call .venv\Scripts\activate.bat
python tools\version_info.py || exit /b 1
pyinstaller --noconfirm --clean --windowed --name SyncLive --icon synclive\assets\synclive.ico ^
  --version-file build\version_info.txt ^
  --add-data "synclive\assets;synclive\assets" ^
  --add-data "models\hand_landmarker.task;models" ^
  --collect-all mediapipe ^
  --collect-submodules av ^
  --exclude-module tkinter --exclude-module pytest ^
  main.py || exit /b 1
python tools\collect_licenses.py || exit /b 1
