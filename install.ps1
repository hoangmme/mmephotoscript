$ErrorActionPreference = "Stop"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  LL PHOTOBOOTH - TU DONG CAI DAT TREN WINDOWS" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

# 1. Kiem tra Python va Git
try {
    $pythonVersion = python --version 2>&1
    Write-Host "[OK] Da tim thay Python: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "[Loi] Khong tim thay Python! Vui long cai dat Python truoc khi tiep tuc." -ForegroundColor Red
    exit
}

try {
    $gitVersion = git --version 2>&1
    Write-Host "[OK] Da tim thay Git: $gitVersion" -ForegroundColor Green
} catch {
    Write-Host "[Loi] Khong tim thay Git! Vui long cai dat Git truoc khi tiep tuc." -ForegroundColor Red
    exit
}

# 2. Xac dinh thu muc cai dat (Thu muc hien tai)
$InstallDir = (Get-Location).Path

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "[*] Dang o san trong thu muc code. Dang cap nhat..." -ForegroundColor Yellow
    git pull
} else {
    Write-Host "[*] Dang tai mmephoto vao thu muc hien tai: $InstallDir..." -ForegroundColor Yellow
    git clone https://github.com/hoangmme/mmephotoscript.git .
}

# 3. Them thu muc cai dat vao PATH (de chay lenh mmephoto o moi noi)
Write-Host "[*] Dang them thu muc code vao he thong (PATH)..." -ForegroundColor Yellow
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable('PATH', "$userPath;$InstallDir", 'User')
}
$env:PATH = "$env:PATH;$InstallDir"

# 4. Chay cac buoc cai dat thong qua mmephoto.bat
Write-Host "[*] Dang cai dat thu vien va tao Startup script..." -ForegroundColor Yellow
cmd.exe /c "mmephoto.bat install"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "[XONG] CAI DAT HOAN TAT!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Dang mo giao dien Dang ky Phong & Thu muc anh..." -ForegroundColor Yellow
cmd.exe /c "mmephoto.bat setup"
