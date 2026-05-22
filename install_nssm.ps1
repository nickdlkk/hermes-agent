$ErrorActionPreference = "Continue"

$report = @()
$report += "=" * 60
$report += "Hermes Gateway NSSM Install Report"
$report += "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$report += "=" * 60

$HERMES_DIR = "C:\Users\Administrator\AppData\Local\hermes\hermes-agent"
$PYTHON_EXE = "$HERMES_DIR\venv\Scripts\python.exe"
$GATEWAY_SCRIPT = "$HERMES_DIR\hermes_cli\main.py"
$HERMES_HOME = "C:\Users\Administrator\AppData\Local\hermes"
$NSSM_DIR = "C:\tools"
$NSSM_EXE = "$NSSM_DIR\nssm.exe"
$SERVICE_NAME = "HermesGateway"

# 1. Check current gateway
$report += "`n[1/6] Checking current gateway..."
$gatewayPidFile = "$env:USERPROFILE\AppData\Local\hermes\gateway.pid"
if (Test-Path $gatewayPidFile) {
    $pidContent = Get-Content $gatewayPidFile -Raw | ConvertFrom-Json
    $gatewayPid = $pidContent.pid
    $report += "  PID file: $gatewayPid"
    $existingProc = Get-Process -Id $gatewayPid -ErrorAction SilentlyContinue
    if ($existingProc) {
        $report += "  Status: Running (PID $gatewayPid)"
    } else {
        $report += "  Status: Process dead"
    }
}

# 2. Check NSSM
$report += "`n[2/6] Checking NSSM..."
if (Test-Path $NSSM_EXE) {
    $report += "  NSSM found: $NSSM_EXE"
} else {
    $report += "  NSSM not found, will download..."
}

# 3. Download NSSM
if (-not (Test-Path $NSSM_EXE)) {
    $report += "`n[3/6] Downloading NSSM..."
    $NSSM_URL = "https://nssm.cc/release/nssm-2.51.zip"
    $NSSM_ZIP = "$env:TEMP\nssm-2.51.zip"
    $NSSM_EXTRACT = "$env:TEMP\nssm"

    try {
        if (-not (Test-Path $NSSM_DIR)) { New-Item -ItemType Directory -Path $NSSM_DIR -Force | Out-Null }
        if (Test-Path $NSSM_ZIP) { Remove-Item $NSSM_ZIP -Force }
        if (Test-Path $NSSM_EXTRACT) { Remove-Item $NSSM_EXTRACT -Recurse -Force }

        $report += "  Downloading: $NSSM_URL"
        Invoke-WebRequest -Uri $NSSM_URL -OutFile $NSSM_ZIP -TimeoutSec 60
        $report += "  Downloaded: $NSSM_ZIP"

        $report += "  Extracting..."
        Expand-Archive -Path $NSSM_ZIP -DestinationPath $NSSM_EXTRACT -Force
        $src = Get-ChildItem "$NSSM_EXTRACT\**\nssm.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($src) {
            Copy-Item $src.FullName $NSSM_DIR -Force
            $report += "  Installed: $NSSM_EXE"
        }
        Remove-Item $NSSM_ZIP -Force
    } catch {
        $report += "  [ERROR] Download failed: $_"
    }
}

# 4. Check Python
$report += "`n[4/6] Checking Python..."
$report += "  Python: $PYTHON_EXE"
if (Test-Path $PYTHON_EXE) {
    $pythonVersion = & $PYTHON_EXE --version 2>&1
    $report += "  Version: $pythonVersion"
} else {
    $report += "  [ERROR] Python not found"
}

$report += "  Gateway script: $GATEWAY_SCRIPT"
if (-not (Test-Path $GATEWAY_SCRIPT)) {
    $report += "  [ERROR] Gateway script not found"
}

# 5. Stop existing gateway
$report += "`n[5/6] Stopping existing gateway..."
if ($gatewayPid) {
    try {
        Stop-Process -Id $gatewayPid -Force -ErrorAction Stop
        $report += "  Stopped PID: $gatewayPid"
    } catch {
        $report += "  Failed to stop: $_"
    }
}
if (Test-Path $gatewayPidFile) {
    Remove-Item $gatewayPidFile -Force
    $report += "  Removed PID file"
}

# 6. Install NSSM service
$report += "`n[6/6] Installing NSSM service..."

try {
    & $NSSM_EXE stop $SERVICE_NAME 2>$null
    & $NSSM_EXE remove $SERVICE_NAME 2>$null
} catch {}

$report += "  Installing: $SERVICE_NAME"
& $NSSM_EXE install $SERVICE_NAME $PYTHON_EXE "$GATEWAY_SCRIPT gateway"
& $NSSM_EXE set $SERVICE_NAME AppDirectory $HERMES_DIR
& $NSSM_EXE set $SERVICE_NAME AppEnvironment "HERMES_HOME=$HERMES_HOME" "_HERMES_GATEWAY=1"
& $NSSM_EXE set $SERVICE_NAME DisplayName "Hermes Agent Gateway"
& $NSSM_EXE set $SERVICE_NAME Description "Hermes Agent Messaging Gateway"
& $NSSM_EXE set $SERVICE_NAME Start SERVICE_AUTO_START
$report += "  Service registered"

# Start service
$report += "`n  Starting service..."
$startResult = & $NSSM_EXE start $SERVICE_NAME 2>&1
$report += "  Start result: $startResult"

Start-Sleep -Seconds 3

# Verify
$report += "`n[Verification] Service status..."
$status = & $NSSM_EXE status $SERVICE_NAME 2>&1
$report += "  Status: $status"

$svcProc = Get-CimInstance Win32_Service -Filter "Name='$SERVICE_NAME'" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($svcProc) {
    $report += "  ServiceType: $($svcProc.ServiceType)"
    $report += "  StartMode: $($svcProc.StartMode)"
    $report += "  State: $($svcProc.State)"
    $report += "  ProcessId: $($svcProc.ProcessId)"
}

# NSSM config details
$report += "`n[NSSM Configuration]"
$report += "  Binary: $(& $NSSM_EXE get $SERVICE_NAME Application 2>&1)"
$report += "  AppDirectory: $(& $NSSM_EXE get $SERVICE_NAME AppDirectory 2>&1)"
$report += "  DisplayName: $(& $NSSM_EXE get $SERVICE_NAME DisplayName 2>&1)"
$report += "  Description: $(& $NSSM_EXE get $SERVICE_NAME Description 2>&1)"
$report += "  Env: $(& $NSSM_EXE get $SERVICE_NAME AppEnvironment 2>&1)"

$report += "`n" + "=" * 60
$report += "Installation Complete"
$report += "=" * 60

# Save report
$report | ForEach-Object { Write-Host $_ }

$reportPath = "D:\AI\hermes_gateway_nssm_install_report.txt"
$report | Out-File -FilePath $reportPath -Encoding UTF8
Write-Host "`nReport saved: $reportPath"
