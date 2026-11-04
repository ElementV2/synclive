@echo off
REM Construit dist\SyncLive\SyncLive.exe
call .venv\Scripts\activate.bat
pyinstaller --noconfirm --clean --windowed --name SyncLive --icon synclive\assets\synclive.ico ^
  --add-data "synclive\assets;synclive\assets" ^
  --add-data "models\hand_landmarker.task;models" ^
  --collect-all mediapipe ^
  --collect-submodules av ^
  --exclude-module tkinter --exclude-module pytest ^
  main.py
