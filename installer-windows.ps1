<#
.SYNOPSIS
    Installation complète sur Windows : Docker (si absent) puis l'application de synchronisation.

.DESCRIPTION
    1. Vérifie Docker ; s'il est absent, active WSL 2 et installe Docker Desktop.
    2. Bascule Docker en conteneurs Linux si nécessaire.
    3. Crée le fichier .env (mot de passe admin demandé, clé secrète générée).
    4. Construit et démarre l'application (compose.serveur.yml).
    5. Ouvre le port dans le pare-feu et affiche l'adresse du tableau de bord.

    Le script peut être relancé sans risque : il sert aussi à mettre à jour l'application.

.EXAMPLE
    Double-cliquez sur installer-windows.bat

.EXAMPLE
    .\installer-windows.ps1 -Port 8080 -OpenDatabasePorts
#>
param(
    # Port du tableau de bord.
    [int]$Port = 8000,
    # Fuseau horaire d'affichage des dates.
    [string]$TimeZone = "Africa/Dakar",
    # Ouvre aussi 3306 (MariaDB) et 5432 (PostgreSQL) dans le pare-feu, si les bases sont sur ce serveur.
    [switch]$OpenDatabasePorts,
    # N'ajoute aucune règle de pare-feu.
    [switch]$NoFirewall,
    # Sur Windows Server, installe Docker Desktop sans demander de confirmation.
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --------------------------------------------------------------------------- droits administrateur

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Relance du script en tant qu'administrateur..." -ForegroundColor Yellow
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                 "-Port", $Port, "-TimeZone", $TimeZone)
    if ($OpenDatabasePorts) { $argList += "-OpenDatabasePorts" }
    if ($NoFirewall) { $argList += "-NoFirewall" }
    if ($Force) { $argList += "-Force" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

$Root = $PSScriptRoot
Set-Location $Root
$ComposeFile = Join-Path $Root "compose.serveur.yml"
$EnvFile = Join-Path $Root ".env"
$DockerDesktopExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
$DockerCliExe = Join-Path $env:ProgramFiles "Docker\Docker\DockerCli.exe"
$DockerDesktopUrl = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"

Start-Transcript -Path (Join-Path $Root "installation.log") -Append | Out-Null

# --------------------------------------------------------------------------- affichage

function Write-Step([string]$Number, [string]$Message) {
    Write-Host ""
    Write-Host "[$Number] $Message" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) { Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    !   $Message" -ForegroundColor Yellow }
function Stop-Install([string]$Message) {
    Write-Host ""
    Write-Host "ERREUR : $Message" -ForegroundColor Red
    Write-Host "Détails dans installation.log" -ForegroundColor Red
    Stop-Transcript | Out-Null
    Read-Host "Appuyez sur Entrée pour fermer"
    exit 1
}

# --------------------------------------------------------------------------- outils Docker

# Exécute une commande externe sans que sa sortie d'erreur interrompe le script.
function Invoke-Native([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return @(& $Command 2>&1 | ForEach-Object { "$_" })
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $dockerBin = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"
    if ((Test-Path $dockerBin) -and ($env:Path -notlike "*$dockerBin*")) { $env:Path += ";$dockerBin" }
}

function Test-DockerCommand { return [bool](Get-Command docker -ErrorAction SilentlyContinue) }

function Get-DockerOsType {
    if (-not (Test-DockerCommand)) { return $null }
    $out = Invoke-Native { docker info --format "{{.OSType}}" }
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($out | Select-Object -Last 1).Trim()
}

function Wait-Docker([int]$TimeoutSeconds = 300, [string]$ExpectedOsType = "") {
    Write-Host "    Attente de Docker (jusqu'à $([int]($TimeoutSeconds / 60)) min)" -NoNewline
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $current = Get-DockerOsType
        if ($current -and (-not $ExpectedOsType -or $current -eq $ExpectedOsType)) { Write-Host ""; return $true }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    Write-Host ""
    return $false
}

function Start-DockerDesktop {
    if (-not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
        Start-Process $DockerDesktopExe
    }
    if (-not (Wait-Docker 300)) {
        Stop-Install ("Docker ne répond pas. Ouvrez Docker Desktop, acceptez les conditions s'il les affiche, " +
                      "attendez qu'il indique 'Engine running', puis relancez ce script.")
    }
}

function Register-ResumeAfterReboot {
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port -TimeZone $TimeZone"
    if ($OpenDatabasePorts) { $command += " -OpenDatabasePorts" }
    if ($NoFirewall) { $command += " -NoFirewall" }
    if ($Force) { $command += " -Force" }
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce" `
                     -Name "InstallationSynchro" -Value $command
}

function Enable-Wsl {
    Write-Step "1a" "Activation de WSL 2 (nécessaire à Docker)"
    $restartNeeded = $false
    foreach ($feature in "Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform") {
        $state = Get-WindowsOptionalFeature -Online -FeatureName $feature
        if ($state.State -ne "Enabled") {
            Write-Host "    Activation de $feature..."
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart
            if ($result.RestartNeeded) { $restartNeeded = $true }
        }
    }
    if ($restartNeeded) {
        Register-ResumeAfterReboot
        Write-Warn "Windows doit redémarrer pour terminer l'activation de WSL 2."
        Write-Warn "L'installation reprendra automatiquement à la prochaine ouverture de session."
        $answer = Read-Host "Redémarrer maintenant ? (O/n)"
        Stop-Transcript | Out-Null
        if ($answer -notmatch "^[nN]") { Restart-Computer -Force }
        exit 0
    }
    Invoke-Native { wsl --update } | Out-Null
    Invoke-Native { wsl --set-default-version 2 } | Out-Null
    Write-Ok "WSL 2 activé."
}

function Install-DockerDesktop {
    $os = Get-CimInstance Win32_OperatingSystem
    if ($os.ProductType -ne 1 -and -not $Force) {
        Write-Warn "Ce serveur est un Windows Server : Docker Desktop n'y est pas officiellement supporté"
        Write-Warn "(il fonctionne en général sur Windows Server 2022 avec WSL 2)."
        $answer = Read-Host "Installer Docker Desktop quand même ? (o/N)"
        if ($answer -notmatch "^[oOyY]") {
            Stop-Install "Installation annulée. Installez Docker avec conteneurs Linux puis relancez ce script."
        }
    }

    Enable-Wsl

    Write-Step "1b" "Téléchargement de Docker Desktop (environ 500 Mo)"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Join-Path $env:TEMP "DockerDesktopInstaller.exe"
    Invoke-WebRequest -UseBasicParsing -Uri $DockerDesktopUrl -OutFile $installer
    Write-Ok "Téléchargé."

    Write-Step "1c" "Installation de Docker Desktop (quelques minutes)"
    $process = Start-Process $installer -Wait -PassThru -ArgumentList `
        "install", "--quiet", "--accept-license", "--backend=wsl-2", "--always-run-service"
    if ($process.ExitCode -ne 0) {
        Stop-Install "L'installation de Docker Desktop a échoué (code $($process.ExitCode))."
    }
    Remove-Item $installer -ErrorAction SilentlyContinue
    try {
        Add-LocalGroupMember -Group "docker-users" -Member "$env:USERDOMAIN\$env:USERNAME" -ErrorAction Stop
    } catch { }
    Update-SessionPath
    Write-Ok "Docker Desktop installé."
}

function Enable-DockerDesktopAutoStart {
    # Docker Desktop démarre à l'ouverture de session : on s'assure qu'il est lancé automatiquement.
    $taskName = "Docker Desktop - demarrage automatique"
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        $action = New-ScheduledTaskAction -Execute $DockerDesktopExe
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Description "Lance Docker Desktop pour l'application de synchronisation" | Out-Null
    }
    Write-Ok "Docker Desktop démarrera automatiquement à l'ouverture de session."
}

# --------------------------------------------------------------------------- .env

function Read-AdminPassword {
    while ($true) {
        $first = Read-Host "    Mot de passe du compte 'admin' du tableau de bord (8 caractères min.)" -AsSecureString
        $second = Read-Host "    Confirmez le mot de passe" -AsSecureString
        $plain1 = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($first))
        $plain2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($second))
        if ($plain1 -ne $plain2) { Write-Warn "Les mots de passe sont différents."; continue }
        if ($plain1.Length -lt 8) { Write-Warn "8 caractères minimum."; continue }
        if ($plain1.Contains("'")) { Write-Warn "L'apostrophe (') n'est pas autorisée."; continue }
        return $plain1
    }
}

function New-SecretKey {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Write-EnvFile([string[]]$Lines) {
    # UTF-8 sans BOM : Docker Compose ne lit pas correctement un fichier .env avec BOM.
    [IO.File]::WriteAllLines($EnvFile, $Lines, (New-Object Text.UTF8Encoding($false)))
}

# =========================================================================== installation

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " Installation - Synchronisation MariaDB -> PostgreSQL" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

if (-not (Test-Path $ComposeFile) -or -not (Test-Path (Join-Path $Root "Dockerfile"))) {
    Stop-Install "Lancez ce script depuis le dossier de l'application (Dockerfile et compose.serveur.yml introuvables)."
}

# ---- 1. Docker
Write-Step "1" "Vérification de Docker"
Update-SessionPath
if (Get-DockerOsType) {
    Write-Ok "Docker est installé et démarré."
} elseif (Test-Path $DockerDesktopExe) {
    Write-Host "    Docker Desktop est installé mais arrêté : démarrage..."
    Start-DockerDesktop
} elseif (Get-Service docker -ErrorAction SilentlyContinue) {
    Write-Host "    Service Docker arrêté : démarrage..."
    Start-Service docker
    if (-not (Wait-Docker 120)) { Stop-Install "Le service Docker ne démarre pas." }
} else {
    Write-Warn "Docker n'est pas installé : installation de Docker Desktop."
    Install-DockerDesktop
    Start-DockerDesktop
}

$osType = Get-DockerOsType
if ($osType -ne "linux") {
    if (Test-Path $DockerCliExe) {
        Write-Host "    Docker est en mode conteneurs Windows : bascule en conteneurs Linux..."
        Invoke-Native { & $DockerCliExe -SwitchLinuxEngine } | Out-Null
        if (-not (Wait-Docker 180 "linux")) {
            Stop-Install "Impossible de basculer en conteneurs Linux. Faites-le depuis l'icône Docker (Switch to Linux containers)."
        }
    } else {
        Stop-Install ("Docker fonctionne en conteneurs Windows et ne peut pas exécuter de conteneurs Linux. " +
                      "Installez Docker Desktop (ou Docker dans WSL 2) puis relancez ce script.")
    }
}
if (Test-Path $DockerDesktopExe) { Enable-DockerDesktopAutoStart }
$composeVersion = Invoke-Native { docker compose version --short }
if ($LASTEXITCODE -ne 0) { Stop-Install "La commande 'docker compose' est indisponible. Mettez Docker à jour." }
Write-Ok "Docker prêt (conteneurs Linux, Compose $($composeVersion | Select-Object -Last 1))."

# ---- 2. Configuration
Write-Step "2" "Configuration (.env)"
if (Test-Path $EnvFile) {
    $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^\s*APP_PORT\s*=" })
    $portLine = Get-Content $EnvFile | Where-Object { $_ -match "^\s*APP_PORT\s*=" } | Select-Object -First 1
    if (-not $PSBoundParameters.ContainsKey("Port") -and $portLine -match "(\d+)") { $Port = [int]$Matches[1] }
    Write-EnvFile ($lines + "APP_PORT=$Port")
    Write-Ok "Fichier .env existant conservé."
} else {
    $password = Read-AdminPassword
    Write-EnvFile @(
        "# Généré par installer-windows.ps1",
        "ADMIN_USERNAME=admin",
        "ADMIN_PASSWORD='$password'",
        "SECRET_KEY=$(New-SecretKey)",
        "APP_TIMEZONE=$TimeZone",
        "APP_PORT=$Port",
        "LOG_LEVEL=INFO",
        "LOG_RETENTION_DAYS=30",
        "BATCH_SIZE=5000"
    )
    Write-Ok "Fichier .env créé (identifiant : admin)."
}

# ---- 3. Application
Write-Step "3" "Construction et démarrage de l'application (quelques minutes la première fois)"
$output = Invoke-Native { docker compose -f $ComposeFile up -d --build }
$output | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) { Stop-Install "Le démarrage de l'application a échoué (voir les messages ci-dessus)." }

$url = "http://localhost:$Port"
Write-Host "    Vérification de $url" -NoNewline
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/health" -TimeoutSec 5
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host ""
if (-not $healthy) {
    Stop-Install "L'application ne répond pas. Consultez : docker compose -f compose.serveur.yml logs"
}
Write-Ok "Application démarrée."

# ---- 4. Pare-feu
if (-not $NoFirewall) {
    Write-Step "4" "Pare-feu Windows"
    $rules = @(@{ Name = "Synchro - tableau de bord ($Port)"; Port = $Port })
    if ($OpenDatabasePorts) {
        $rules += @{ Name = "Synchro - MariaDB (3306)"; Port = 3306 }
        $rules += @{ Name = "Synchro - PostgreSQL (5432)"; Port = 5432 }
    }
    foreach ($rule in $rules) {
        if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol TCP `
                -LocalPort $rule.Port -Action Allow | Out-Null
        }
        Write-Ok "Port $($rule.Port) ouvert."
    }
}

# ---- Résumé
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
    $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
    $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*WSL*"
} | Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Green
Write-Host " Installation terminée" -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
Write-Host " Tableau de bord : $url"
foreach ($ip in $ips) { Write-Host "   depuis le réseau : http://${ip}:$Port" }
Write-Host " Identifiant     : admin (mot de passe choisi à l'installation)"
Write-Host ""
Write-Host " Dans 'Connexions', pour une base installée sur CE serveur,"
Write-Host " utilisez l'hôte : host.docker.internal   (et non localhost)"
Write-Host ""
Write-Host " Mise à jour : remplacez les fichiers puis relancez installer-windows.bat"
Write-Host "=================================================================" -ForegroundColor Green

Stop-Transcript | Out-Null
Start-Process $url
Read-Host "Appuyez sur Entrée pour fermer"
