@echo off
cd /d "%~dp0"
REM Mode dev : l'interface se relance toute seule a chaque modification du code
REM   dev.bat                 interface
REM   dev.bat "video.mp4"     interface + analyse automatique de la video a chaque relance
call .venv\Scripts\activate.bat
python tools\dev.py %*
