@echo off
REM ============================================================
REM  Build SmugMug2GDrive.exe
REM  Run this on a Windows machine with Python installed.
REM ============================================================

echo.
echo ==========================================
echo  SmugMug to Google Drive - EXE Builder
echo ==========================================
echo.

REM Step 1: Install dependencies
echo [1/2] Installing dependencies...
pip install requests requests-oauthlib google-auth google-auth-oauthlib google-api-python-client pyinstaller

echo.
echo [2/2] Building executable...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "SmugMug2GDrive" ^
    --add-data "README.md;." ^
    smugmug_to_gdrive_gui.py

echo.
echo ==========================================
if exist "dist\SmugMug2GDrive.exe" (
    echo  SUCCESS! Your .exe is at:
    echo  dist\SmugMug2GDrive.exe
) else (
    echo  BUILD FAILED - check errors above.
)
echo ==========================================
echo.
pause
