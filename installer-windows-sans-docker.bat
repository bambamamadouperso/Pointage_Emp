@echo off
rem Installation SANS Docker (serveur sans virtualisation).
rem Double-cliquez sur ce fichier : les droits administrateur seront demandes.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-windows-sans-docker.ps1" %*
