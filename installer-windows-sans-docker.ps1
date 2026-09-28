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
    [switch]$NoFirewall,
    # Compte Windows sous lequel tourne l'application (par défaut : SYSTEM). Utile quand un pilote ODBC
    # (ex. HFSQL) ne fonctionne qu'avec un vrai compte utilisateur. Ex. : -ServiceAccount "DOMAINE\utilisateur"
    # « SYSTEM » pour revenir au compte système. Le choix est mémorisé dans .env (TASK_ACCOUNT).
    [string]$ServiceAccount = ""
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
    if ($ServiceAccount) { $argList += @("-ServiceAccount", "`"$ServiceAccount`"") }
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
$InstallStartedUtc = (Get-Date).ToUniversalTime().AddSeconds(-5)
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
function Stop-Install([string]$Message, [string]$Details = "") {
    Write-Host ""
    Write-Host "ERREUR : $Message" -ForegroundColor Red
    if ($Details) { Write-Host $Details -ForegroundColor Red }
    # Le message reste lisible même si la fenêtre est fermée : fichier ouvert dans le Bloc-notes.
    $errorFile = Join-Path $PSScriptRoot "installation-erreur.txt"
    try {
        Set-Content -Path $errorFile -Encoding UTF8 -Value @(
            "Erreur d'installation du $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')", "", $Message, "", $Details, "",
            "Journal complet : installation.log")
        Start-Process notepad.exe -ArgumentList "`"$errorFile`""
    } catch { }
    Write-Host "Message enregistré dans installation-erreur.txt (ouvert dans le Bloc-notes)" -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch { }
    Read-Host "Appuyez sur Entrée pour fermer"
    exit 3
}

# Toute erreur imprévue : affichée, enregistrée, et la fenêtre attend avant de se fermer.
trap {
    $info = $_.InvocationInfo
    $where = if ($info -and $info.ScriptLineNumber) { "Ligne $($info.ScriptLineNumber) : $($info.Line.Trim())" } else { "" }
    Stop-Install "Erreur inattendue : $($_.Exception.Message)" $where
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

function Get-PortOwners([int]$LocalPort) {
    return @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 })
}

function Stop-App {
    # Le lanceur (app-service.cmd) relance le serveur s'il s'arrête : ce fichier lui demande de s'arrêter.
    $stopFlag = Join-Path $Root "data\arret.flag"
    New-Item -ItemType Directory -Force -Path (Split-Path $stopFlag) | Out-Null
    Set-Content -Path $stopFlag -Value "arret demande par l'installateur" -Encoding ASCII
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # Sous Windows, .venv\Scripts\python.exe n'est qu'un lanceur : le serveur tourne dans un processus
    # Python enfant (celui de Program Files). On arrête donc tout processus uvicorn de l'application,
    # quel que soit son exécutable, puis ce qui écoute encore sur le port.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine -like "*uvicorn*app.main:app*")
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-PortOwners $Port).Count -gt 0 -and (Get-Date) -lt $deadline) {
        foreach ($processId in Get-PortOwners $Port) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -like "python*") {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 1
    }
    $owners = Get-PortOwners $Port
    if ($owners.Count -gt 0) {
        $names = ($owners | ForEach-Object { (Get-Process -Id $_ -ErrorAction SilentlyContinue).ProcessName }) -join ", "
        Stop-Install ("Le port $Port est occupé par un autre programme ($names). Arrêtez-le ou relancez " +
                      "l'installation avec un autre port : .\installer-windows-sans-docker.ps1 -Port 8080")
    }
}

# Lecture sûre du .env : seuls les 64 premiers Ko sont lus (un fichier abîmé ou démesuré faisait
# planter l'installation par manque de mémoire) et seules les lignes « CLE=valeur » sont gardées.
# Un fichier abîmé est mis de côté (.env.abime-<date>) et remplacé par sa partie valide.
function Read-EnvLines {
    if (-not (Test-Path $EnvFile -PathType Leaf)) { return @() }
    $size = (Get-Item $EnvFile).Length
    $stream = [IO.File]::OpenRead($EnvFile)
    try {
        $buffer = New-Object byte[] ([Math]::Min($size, 65536))
        $read = $stream.Read($buffer, 0, $buffer.Length)
    } finally { $stream.Dispose() }
    $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read).TrimStart([char]0xFEFF)
    $entries = [ordered]@{}
    $damaged = $size -gt 65536 -or $text.IndexOf([char]0) -ge 0
    foreach ($line in ($text -split "[`r`n`0]+")) {
        $clean = $line.Trim()
        if (-not $clean -or $clean.StartsWith("#")) { continue }
        if ($clean.Length -lt 4096 -and $clean -match "^([A-Za-z_][A-Za-z0-9_]*)\s*=") {
            if ($entries.Contains($Matches[1])) { $damaged = $true }
            $entries[$Matches[1]] = $clean
        } else {
            $damaged = $true
        }
    }
    $lines = @($entries.Values)
    if ($damaged) {
        $backup = "$EnvFile.abime-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item -Path $EnvFile -Destination $backup -Force
        Write-EnvFile $lines
        Write-Warn ("Fichier .env abîmé ($([Math]::Round($size / 1KB)) Ko) : réparé ($($lines.Count) paramètres " +
                    "conservés). L'original est dans $(Split-Path $backup -Leaf) (vous pourrez le supprimer).")
    }
    return $lines
}

function Get-EnvValue([string]$Name) {
    $line = Read-EnvLines | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if ($line) { return ($line -split "=", 2)[1].Trim().Trim("'") }
    return ""
}

function Set-EnvValue([string]$Name, [string]$Value) {
    $lines = @(Read-EnvLines | Where-Object { $_ -notmatch "^\s*$Name\s*=" })
    Write-EnvFile ($lines + "$Name=$Value")
}

function Register-App {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $appLog = Join-Path $LogDir "application.log"
    # Lanceur qui relance automatiquement le serveur s'il s'arrête (fenêtre fermée, Ctrl+C, plantage).
    $launcher = Join-Path $Root "app-service.cmd"
    $command = "/c `"`"$launcher`" $Port`""
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $command -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $description = "Synchronisation vers PostgreSQL (tableau de bord port $Port)"

    $account = $ServiceAccount
    if (-not $account) { $account = Get-EnvValue "TASK_ACCOUNT" }
    if (-not $account -or $account -eq "SYSTEM") {
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
            -Settings $settings -Description $description -Force | Out-Null
        Set-EnvValue "TASK_ACCOUNT" "SYSTEM"
        Write-Ok "L'application tourne sous le compte SYSTEM."
        return
    }
    # Compte utilisateur : la tâche démarre avec Windows, même sans session ouverte (mot de passe requis).
    Write-Host "    L'application tournera sous le compte « $account »."
    $credential = Get-Credential -UserName $account -Message "Mot de passe Windows du compte $account (pour lancer l'application au démarrage)"
    if (-not $credential) { Stop-Install "Mot de passe non saisi." }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $credential.UserName -Password $credential.GetNetworkCredential().Password -RunLevel Highest `
        -Description $description -Force | Out-Null
    Set-EnvValue "TASK_ACCOUNT" $credential.UserName
    Write-Ok "L'application tourne sous le compte « $($credential.UserName) »."
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
    $all = @(Read-EnvLines)
    $lines = @($all | Where-Object { $_ -notmatch "^\s*APP_PORT\s*=" })
    $portLine = $all | Where-Object { $_ -match "^\s*APP_PORT\s*=" } | Select-Object -Last 1
    if (-not $PSBoundParameters.ContainsKey("Port") -and $portLine -match "(\d+)") { $Port = [int]$Matches[1] }
    # Paramètres indispensables perdus (fichier abîmé) : on les recrée.
    if (-not ($lines | Where-Object { $_ -match "^\s*ADMIN_USERNAME\s*=" })) { $lines += "ADMIN_USERNAME=admin" }
    if (-not ($lines | Where-Object { $_ -match "^\s*ADMIN_PASSWORD\s*=" })) {
        Write-Warn "Mot de passe du compte 'admin' introuvable dans .env : choisissez-en un nouveau."
        $lines += "ADMIN_PASSWORD='$(Read-AdminPassword)'"
    }
    if (-not ($lines | Where-Object { $_ -match "^\s*SECRET_KEY\s*=" })) { $lines += "SECRET_KEY=$(New-SecretKey)" }
    if (-not ($lines | Where-Object { $_ -match "^\s*APP_TIMEZONE\s*=" })) { $lines += "APP_TIMEZONE=$TimeZone" }
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

# Clé de chiffrement des mots de passe des connexions : si elle a changé (fichier .env recréé),
# l'ancienne est recherchée dans les copies du .env (.env.abime-*) et remise en place.
if (Test-Path (Join-Path $Root "data\app.db")) {
    $out = Invoke-Native { & $VenvPython -m app.recuperer_cle }
    $code = $LASTEXITCODE
    $out | Where-Object { $_ -and $_ -notmatch "^\d{4}-\d\d-\d\d .* (INFO|DEBUG) " } | ForEach-Object { Write-Host "    $_" }
    if ($code -eq 1) { Write-Warn "Mots de passe des connexions à ressaisir dans l'application (voir ci-dessus)." }
    elseif ($code -eq 0) { Write-Ok "Mots de passe des connexions lisibles." }
}

# ---- 4. Démarrage
Write-Step "4" "Démarrage de l'application (tâche Windows au démarrage du serveur)"
Register-App
Remove-Item -Path (Join-Path $Root "data\arret.flag") -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName

$url = "http://localhost:$Port"
Write-Host "    Vérification de $url" -NoNewline
$healthy = $false
for ($i = 0; $i -lt 45; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/health" -TimeoutSec 5
        $health = $response.Content | ConvertFrom-Json
        # On s'assure que c'est bien la nouvelle instance qui répond (démarrée après ce script).
        if ($response.StatusCode -eq 200 -and $health.started_at -and
            [datetime]::Parse($health.started_at).ToUniversalTime() -ge $InstallStartedUtc) {
            $healthy = $true
            break
        }
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
Write-Ok "Application démarrée (version $($health.version), tâche planifiée « $TaskName »)."

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
