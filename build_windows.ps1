# Build MiniCut as Windows executable using Flet pack (PyInstaller underneath).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_windows.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (!(Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $root ".venv\\Scripts\\python.exe"
if (!(Test-Path $py)) { throw "Python venv not found: $py" }

& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt

if (!(Test-Path "bin\ffmpeg.exe") -or !(Test-Path "bin\ffprobe.exe")) {
  Write-Host "WARN: ไม่พบ bin\ffmpeg.exe หรือ bin\ffprobe.exe"
  Write-Host "      กรุณาวางไฟล์ก่อน แล้วค่อย build เพื่อให้ exe พก ffmpeg ไปด้วย"
}

# -D = one-folder bundle (ลดปัญหา antivirus/onefile)
$flet = Join-Path $root ".venv\\Scripts\\flet.exe"
if (!(Test-Path $flet)) { $flet = "flet" }

& $flet pack app.py -n MiniCut -D `
  --add-binary "bin\ffmpeg.exe:bin\ffmpeg.exe:win32" `
  --add-binary "bin\ffprobe.exe:bin\ffprobe.exe:win32"

Write-Host "DONE: ดูผลลัพธ์ที่ dist\MiniCut\"
