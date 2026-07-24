@echo off
cd /d "%~dp0"
chcp 65001 >nul

:: Read first argument
set CMD=%1

if "%CMD%"=="" goto menu
if "%CMD%"=="install" goto install
if "%CMD%"=="update" goto update
if "%CMD%"=="reset" goto reset
if "%CMD%"=="start" goto start
if "%CMD%"=="stop" goto stop
if "%CMD%"=="setup" goto setup
if "%CMD%"=="add_to_path" goto add_to_path
if "%CMD%"=="6" goto add_to_path
if "%CMD%"=="help" goto help

echo [!] Lenh khong hop le! Go "mmephoto help" de xem cac lenh.
goto end

:menu
echo ========================================================
echo       LL PHOTOBOOTH - MMEPHOTO MANAGEMENT SCRIPT
echo ========================================================
echo  1. Cai dat (Install) - Tao Startup chay ngam cung Win
echo  2. Cap nhat (Update) - Tai code moi nhat va Restart
echo  3. Reset cau hinh    - Xoa phong cu va Dang ky lai
echo  4. Start             - Bat script chay ngam
echo  5. Stop              - Tat script dang chay ngam
echo ========================================================
set /p opt="Chon tuy chon (1-5): "
if "%opt%"=="1" goto install
if "%opt%"=="2" goto update
if "%opt%"=="3" goto reset
if "%opt%"=="4" goto start
if "%opt%"=="5" goto stop
goto end

:help
echo Cac lenh ho tro:
echo   mmephoto install  - Cai dat thu vien, tao shortcut Startup
echo   mmephoto update   - Git pull code moi nhat va tu dong Restart
echo   mmephoto setup    - Mo Terminal de nhap thong tin Phong
echo   mmephoto reset    - Xoa file config.json de dang ky lai phong
echo   mmephoto start    - Chay ngam sync_client.py
echo   mmephoto stop     - Dung sync_client.py
goto end

:install
echo [*] Dang cai dat thu vien Python (requests, watchdog, pillow)...
pip install requests watchdog pillow

echo [*] Tao script vbs de chay ngam...
echo Set oShell = CreateObject ("Wscript.Shell") > run_hidden.vbs
echo Dim strArgs >> run_hidden.vbs
echo strArgs = "cmd /c python sync_client.py" >> run_hidden.vbs
echo oShell.Run strArgs, 0, false >> run_hidden.vbs

echo [*] Them vao Startup (Khoi dong cung Windows)...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_DIR%\LL_Photobooth_Sync.lnk
set VBS_PATH=%CD%\run_hidden.vbs
powershell -Command "$wshell = New-Object -ComObject WScript.Shell; $shortcut = $wshell.CreateShortcut('%SHORTCUT_PATH%'); $shortcut.TargetPath = 'wscript.exe'; $shortcut.Arguments = '\"%VBS_PATH%\"'; $shortcut.WorkingDirectory = '%CD%'; $shortcut.Save()"

echo.
echo [OK] DA CAI DAT XONG! 
echo      Neu lan dau, hay chay file "sync_client.py" de dien Thong tin chi nhanh.
if "%CMD%"=="" pause
goto end

:update
echo [*] Dang dung sync_client.py de update...
wmic process where "name='python.exe' and commandline like '%%sync_client.py%%'" call terminate >nul 2>&1
wmic process where "name='wscript.exe' and commandline like '%%run_hidden.vbs%%'" call terminate >nul 2>&1

echo [*] Dang lay code moi nhat tu Github...
git pull

echo [*] Cap nhat hoan tat! Khoi dong lai background service...
goto start

:reset
echo [*] Dang dung service...
wmic process where "name='python.exe' and commandline like '%%sync_client.py%%'" call terminate >nul 2>&1

echo [*] Dang xoa cau hinh cu...
if exist config.json del config.json
if exist batch_history.json del batch_history.json

echo [OK] Đã xóa config! Mo giao dien de ban dang ky lai phong...
start cmd /k "python sync_client.py"
goto end

:start
echo [*] Dang chay ngam sync_client.py...
if exist run_hidden.vbs (
    start wscript.exe run_hidden.vbs
    echo [OK] Da bat!
) else (
    echo [!] Vui long chay chuc nang Cai dat (Install) truoc!
)
if "%CMD%"=="" pause
goto end

:stop
echo [*] Dang tat ngam sync_client.py...
wmic process where "name='python.exe' and commandline like '%%sync_client.py%%'" call terminate >nul 2>&1
wmic process where "name='wscript.exe' and commandline like '%%run_hidden.vbs%%'" call terminate >nul 2>&1
echo [OK] Da tat!
if "%CMD%"=="" pause
goto end

:setup
echo [*] Dang mo giao dien de Dang ky Thong tin Phong...
start cmd /k "python sync_client.py"
goto end

:end
