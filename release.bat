@echo off
REM Construit l'exe puis prepare release\ : exe + licences, archive des sources, empreintes SHA-256
call build.bat || exit /b 1
call .venv\Scripts\activate.bat
python tools\make_release.py
