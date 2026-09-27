@echo off
rem Diagnostic de la connexion HFSQL (a lancer dans une session Windows ouverte).
rem Usage : diagnostic-hfsql.bat "Nom de la connexion"   (sans nom : premiere connexion HFSQL)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m app.diag_hfsql %*
echo.
echo Code de sortie : %errorlevel%   (0 = termine normalement ; autre valeur = arret brutal du pilote)
pause
