@echo off
rem Lance le serveur web de l'application et le relance automatiquement s'il s'arrete
rem (fenetre fermee, Ctrl+C, plantage...). L'installateur cree data\arret.flag pour l'arreter vraiment.
rem Utilisation : app-service.cmd <port>
setlocal
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8000
if not exist logs mkdir logs
:boucle
if exist "data\arret.flag" exit /b 0
echo %date% %time% Demarrage du serveur sur le port %PORT% >> "logs\application.log"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --workers 1 --no-access-log --env-file ".env" >> "logs\application.log" 2>&1
echo %date% %time% Serveur arrete (code %errorlevel%) : redemarrage dans 10 s >> "logs\application.log"
rem "ping" sert de pause : "timeout" ne fonctionne pas sans console (tache planifiee).
ping -n 11 127.0.0.1 >nul
goto boucle
