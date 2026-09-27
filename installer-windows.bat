@echo off
rem Installation de l'application de synchronisation (Docker compris).
rem Double-cliquez sur ce fichier : les droits administrateur seront demandes.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-windows.ps1" %*
