# Build MiniCut as Windows executable using Flet pack (PyInstaller underneath).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_windows.ps1

$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv")) { python -m venv .venv }
. .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt

if (!(Test-Path "bin\ffmpeg.exe") -or !(Test-Path "bin\ffprobe.exe")) {
  Write-Host "WARN: ไม่พบ bin\ffmpeg.exe หรือ bin\ffprobe.exe"
  Write-Host "      กรุณาวางไฟล์ก่อน แล้วค่อย build เพื่อให้ exe พก ffmpeg ไปด้วย"
}

# -D = one-folder bundle (ลดปัญหา antivirus/onefile)
flet pack app.py -n MiniCut -D `
  --add-binary "bin\ffmpeg.exe:bin\ffmpeg.exe:win32" `
  --add-binary "bin\ffprobe.exe:bin\ffprobe.exe:win32"

Write-Host "DONE: ดูผลลัพธ์ที่ dist\MiniCut\"
