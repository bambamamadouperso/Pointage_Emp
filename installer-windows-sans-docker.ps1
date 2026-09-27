<#
.SYNOPSIS
    Installation sur Windows SANS Docker (serveur sans virtualisation, machine virtuelle...).

.DESCRIPTION
    1. Installe Python 3.12 s'il est absent.
    2. Crée un environnement Python dédié (.venv) et installe les dépendances.
    3. Crée le fichier .env (mot de passe admin demandé, clé secrète générée).
    4. Enregistre l'application comme tâche Windows lancée au démarrage du serveur
       (compte SYSTEM, relance automatique en cas d'arrêt) : aucune session ouverte n'est nécessaire.
    5. Ouvre le port dans le pare-feu et affiche l'adresse du tableau de bord.

    Le script peut être relancé sans risque : il sert aussi à mettre à jour l'application.

.EXAMPLE
    Double-cliquez sur installer-windows-sans-docker.bat
#>
param(
    [int]$Port = 8000,
    [string]$TimeZone = "Africa/Dakar",
    [switch]$NoFirewall
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
    if ($NoFirewall) { $argList += "-NoFirewall" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

# Désactive le mode "Sélection" de la console : un clic dans la fenêtre ne met plus le script en pause.
try {
    Add-Type -Namespace Console -Name Mode -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int h);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
    $handle = [Console.Mode]::GetStdHandle(-10)
    $mode = 0
    if ([Console.Mode]::GetConsoleMode($handle, [ref]$mode)) {
        [void][Console.Mode]::SetConsoleMode($handle, ($mode -band (-bnot 0x0040)) -bor 0x0080)
    }
} catch { }

$Root = $PSScriptRoot
Set-Location $Root
$EnvFile = Join-Path $Root ".env"
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$TaskName = "Synchro MariaDB-PostgreSQL"
$PythonVersion = "3.12.8"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"

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

# --------------------------------------------------------------------------- Python

# Renvoie le chemin d'un python.exe 3.10 à 3.13 utilisable, ou $null.
function Find-Python {
    $candidates = @()
    foreach ($version in "3.12", "3.13", "3.11", "3.10") {
        $nodot = $version.Replace(".", "")
        $candidates += Join-Path $env:ProgramFiles "Python$nodot\python.exe"
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$nodot\python.exe"
    }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    # Le "python.exe" du Microsoft Store (WindowsApps) n'est qu'un raccourci d'installation : on l'ignore.
    if ($onPath -and $onPath.Source -notlike "*WindowsApps*") { $candidates += $onPath.Source }

    foreach ($path in $candidates) {
        if (-not (Test-Path $path)) { continue }
        $out = Invoke-Native { & $path -c "import sys; print('%d.%d' % sys.version_info[:2])" }
        if ($LASTEXITCODE -ne 0) { continue }
        try { $version = [version]($out | Select-Object -Last 1) } catch { continue }
        if ($version -ge [version]"3.10" -and $version -lt [version]"3.14") { return $path }
    }
    return $null
}

function Install-Python {
    Write-Host "    Téléchargement de Python $PythonVersion..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Invoke-WebRequest -UseBasicParsing -Uri $PythonUrl -OutFile $installer
    Write-Host "    Installation de Python (1 à 2 min)..."
    $process = Start-Process $installer -Wait -PassThru -ArgumentList `
        "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0", "Include_launcher=1"
    if ($process.ExitCode -ne 0) { Stop-Install "L'installation de Python a échoué (code $($process.ExitCode))." }
    Remove-Item $installer -ErrorAction SilentlyContinue
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
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
    [IO.File]::WriteAllLines($EnvFile, $Lines, (New-Object Text.UTF8Encoding($false)))
}

# --------------------------------------------------------------------------- service

function Stop-App {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # Arrête aussi un éventuel processus encore lancé depuis ce dossier.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

function Register-App {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $appLog = Join-Path $LogDir "application.log"
    $command = "/c `"`"$VenvPython`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port " +
               "--workers 1 --no-access-log --env-file `"$EnvFile`" >> `"$appLog`" 2>&1`""
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $command -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "Synchronisation MariaDB -> PostgreSQL (tableau de bord port $Port)" `
        -Force | Out-Null
}

# =========================================================================== installation

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " Installation SANS Docker - Synchronisation MariaDB -> PostgreSQL" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $Root "app\main.py")) -or -not (Test-Path (Join-Path $Root "requirements.txt"))) {
    Stop-Install "Lancez ce script depuis le dossier de l'application (app\main.py introuvable)."
}

# ---- 1. Python
Write-Step "1" "Python"
$python = Find-Python
if ($python) {
    Write-Ok "Python trouvé : $python"
} else {
    Install-Python
    $python = Find-Python
    if (-not $python) { Stop-Install "Python est installé mais introuvable. Redémarrez puis relancez ce script." }
    Write-Ok "Python installé : $python"
}

# ---- 2. Dépendances
Write-Step "2" "Installation des dépendances (1 à 3 min)"
Stop-App
if (-not (Test-Path $VenvPython)) {
    $out = Invoke-Native { & $python -m venv $VenvDir }
    if ($LASTEXITCODE -ne 0) { $out | ForEach-Object { Write-Host "    $_" }; Stop-Install "Création de l'environnement Python impossible." }
}
Invoke-Native { & $VenvPython -m pip install --upgrade pip --disable-pip-version-check -q } | Out-Null
$out = Invoke-Native { & $VenvPython -m pip install -r (Join-Path $Root "requirements.txt") --disable-pip-version-check -q }
if ($LASTEXITCODE -ne 0) {
    $out | ForEach-Object { Write-Host "    $_" }
    Stop-Install "L'installation des dépendances a échoué (accès Internet nécessaire)."
}
Write-Ok "Dépendances installées."

# ---- 3. Configuration
Write-Step "3" "Configuration (.env)"
if (Test-Path $EnvFile) {
    $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^\s*APP_PORT\s*=" })
    $portLine = Get-Content $EnvFile | Where-Object { $_ -match "^\s*APP_PORT\s*=" } | Select-Object -First 1
    if (-not $PSBoundParameters.ContainsKey("Port") -and $portLine -match "(\d+)") { $Port = [int]$Matches[1] }
    Write-EnvFile ($lines + "APP_PORT=$Port")
    Write-Ok "Fichier .env existant conservé."
} else {
    $password = Read-AdminPassword
    Write-EnvFile @(
        "# Généré par installer-windows-sans-docker.ps1",
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

# ---- 4. Démarrage
Write-Step "4" "Démarrage de l'application (tâche Windows au démarrage du serveur)"
Register-App
Start-ScheduledTask -TaskName $TaskName

$url = "http://localhost:$Port"
Write-Host "    Vérification de $url" -NoNewline
$healthy = $false
for ($i = 0; $i -lt 45; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/health" -TimeoutSec 5
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host ""
if (-not $healthy) {
    $appLog = Join-Path $LogDir "application.log"
    if (Test-Path $appLog) { Get-Content $appLog -Tail 20 | ForEach-Object { Write-Host "    $_" } }
    Stop-Install "L'application ne répond pas. Voir logs\application.log"
}
Write-Ok "Application démarrée (tâche planifiée « $TaskName »)."

# ---- 5. Pare-feu
if (-not $NoFirewall) {
    Write-Step "5" "Pare-feu Windows"
    $ruleName = "Synchro - tableau de bord ($Port)"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Action Allow | Out-Null
    }
    Write-Ok "Port $Port ouvert."
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
Write-Host " Pour une base installée sur CE serveur, utilisez l'hôte : localhost"
Write-Host " L'application démarre seule avec Windows (aucune session nécessaire)."
Write-Host " Journal technique : logs\application.log"
Write-Host " Mise à jour : remplacez les fichiers puis relancez installer-windows-sans-docker.bat"
Write-Host "=================================================================" -ForegroundColor Green

Stop-Transcript | Out-Null
Start-Process $url
Read-Host "Appuyez sur Entrée pour fermer"
