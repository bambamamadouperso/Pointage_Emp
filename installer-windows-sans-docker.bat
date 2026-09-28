@echo off
rem Installation SANS Docker (serveur sans virtualisation).
rem Double-cliquez sur ce fichier : les droits administrateur seront demandes.
cd /d "%~dp0"

rem Droits administrateur : relance de CE fichier (la fenetre reste ouverte en cas d'erreur).
net session >nul 2>&1
if errorlevel 1 (
    echo Demande des droits administrateur...
    if "%~1"=="" (
        powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-windows-sans-docker.ps1" %*
set CODE=%errorlevel%
rem Code 3 : l'erreur a deja ete affichee par le script (avec pause).
if not "%CODE%"=="0" if not "%CODE%"=="3" (
    echo.
    echo *** L'installation s'est arretee sur une erreur (code %CODE%^). ***
    echo Le message est affiche ci-dessus ; le detail est dans installation.log
    echo.
    pause
)
