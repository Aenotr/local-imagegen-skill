# Ensure the local ComfyUI server is running; start it if it is not.
#
#   pwsh -File ensure_server.ps1
#
# Exits 0 when the server answers, 1 when it could not be started.
# Run this as a DSH background job (`run_in_background: true`) if ComfyUI needs
# to keep running after the call returns.

param(
    [int]$Port = 8188,
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = 'Stop'
$Root = if ($env:LOCAL_IMAGEGEN_ROOT) { $env:LOCAL_IMAGEGEN_ROOT } else { 'D:\dsh\ComfyUI' }
$Host_ = if ($env:LOCAL_IMAGEGEN_HOST) { $env:LOCAL_IMAGEGEN_HOST } else { "http://127.0.0.1:$Port" }
$Python = Join-Path $Root 'venv\Scripts\python.exe'

function Test-Comfy {
    try {
        $r = Invoke-RestMethod -Uri "$Host_/system_stats" -TimeoutSec 5 -ErrorAction Stop
        return $r
    } catch {
        return $null
    }
}

$stats = Test-Comfy
if ($stats) {
    $dev = $stats.devices[0]
    Write-Output "already running: $Host_"
    Write-Output ("device={0} vram_free={1:N2}GB" -f $dev.name, ($dev.vram_free / 1GB))
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Error "ComfyUI python not found at $Python (set LOCAL_IMAGEGEN_ROOT)"
    exit 1
}

# Keep temp inside the install tree's parent: the DSH sandbox denies writes to
# newly created temp directories outside the workspace.
$TempDir = Join-Path (Split-Path -Parent $Root) '.tmp'
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$env:TMP = $TempDir
$env:TEMP = $TempDir

Write-Output "starting ComfyUI from $Root ..."
Start-Process -FilePath $Python `
    -ArgumentList @((Join-Path $Root 'main.py'), '--listen', '127.0.0.1',
                    '--port', "$Port", '--preview-method', 'none') `
    -WorkingDirectory $Root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $stats = Test-Comfy
    if ($stats) {
        $dev = $stats.devices[0]
        Write-Output "ready: $Host_"
        Write-Output ("device={0} vram_free={1:N2}GB" -f $dev.name, ($dev.vram_free / 1GB))
        exit 0
    }
}

Write-Error "ComfyUI did not become ready within $TimeoutSeconds s"
exit 1
