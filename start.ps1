# ── Configuration ────────────────────────────────────────────────────────────
$env:RTSP_URL = "rtsp://USER:PASS@192.168.1.2:554/h264Preview_01_sub"
$ScriptUrl    = "https://raw.githubusercontent.com/ddshd/vlc-fullscreen-rtsp-viewer/refs/heads/main/viewer.py"
$ReqsUrl      = "https://raw.githubusercontent.com/ddshd/vlc-fullscreen-rtsp-viewer/refs/heads/main/requirements-windows.txt"
$ScriptPath   = Join-Path $PSScriptRoot "viewer.py"
$ReqsPath     = Join-Path $PSScriptRoot "requirements-windows.txt"

# ── Helper: download with fallback ────────────────────────────────────────────
function Download-WithFallback {
    param(
        [string]$Url,
        [string]$Destination,
        [string]$Label
    )

    $Temp = "$Destination.tmp"
    Write-Host "Downloading latest $Label..."

    try {
        Invoke-WebRequest -Uri $Url -OutFile $Temp -UseBasicParsing -ErrorAction Stop
        Move-Item -Path $Temp -Destination $Destination -Force
        Write-Host "Download successful."
    } catch {
        Write-Host "Download failed: $_"
        if (Test-Path $Temp) { Remove-Item $Temp -Force }

        if (Test-Path $Destination) {
            Write-Host "Falling back to existing $Label on disk."
        } else {
            Write-Host "No existing $Label found. Cannot continue."
            exit 1
        }
    }
}

# ── Download files ────────────────────────────────────────────────────────────
Download-WithFallback -Url $ReqsUrl    -Destination $ReqsPath   -Label "requirements-windows.txt"
Download-WithFallback -Url $ScriptUrl  -Destination $ScriptPath -Label "viewer.py"

# ── Install dependencies ──────────────────────────────────────────────────────
Write-Host "Installing required packages..."
pip install -r $ReqsPath --quiet

# ── Launch ────────────────────────────────────────────────────────────────────
Write-Host "Launching viewer..."
Start-Process pythonw -ArgumentList "`"$ScriptPath`""