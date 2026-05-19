@echo off
REM JARVIS — kurulum scripti.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo [JARVIS] .venv zaten mevcut. Bagimliliklar guncelleniyor...
    .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
    set PLAYWRIGHT_BROWSERS_PATH=runtime\ms-playwright
    if not exist "runtime\ms-playwright" mkdir "runtime\ms-playwright"
    dir /b "runtime\ms-playwright\chromium-*" >nul 2>nul
    if errorlevel 1 (
        echo [JARVIS] Playwright Chromium yukleniyor...
        .venv\Scripts\python.exe -m playwright install chromium
    )
    echo [JARVIS] Guncelleme tamam.
    pause
    exit /b 0
)

echo [JARVIS] .venv olusturuluyor...
py -3.12 -m venv .venv 2>nul
if errorlevel 1 (
    py -m venv .venv
    if errorlevel 1 (
        echo [JARVIS] Python bulunamadi. Lutfen Python 3.12 yukleyin:
        echo   winget install Python.Python.3.12
        pause
        exit /b 1
    )
)

echo [JARVIS] pip guncelleniyor...
.venv\Scripts\python.exe -m pip install --upgrade pip wheel setuptools --quiet

echo [JARVIS] Bagimliliklar yukleniyor (3-5 dakika surebilir)...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [JARVIS] Bagimlilik yuklemesi basarisiz.
    pause
    exit /b 1
)

set PLAYWRIGHT_BROWSERS_PATH=runtime\ms-playwright
if not exist "runtime\ms-playwright" mkdir "runtime\ms-playwright"
dir /b "runtime\ms-playwright\chromium-*" >nul 2>nul
if errorlevel 1 (
    echo [JARVIS] Playwright Chromium yukleniyor...
    .venv\Scripts\python.exe -m playwright install chromium
    if errorlevel 1 (
        echo [JARVIS] Playwright Chromium yuklemesi basarisiz. Browser automation daha sonra tekrar kurulabilir.
    )
)

echo.
echo [JARVIS] Kurulum tamam. Calistirmak icin run.bat kullanin.
echo.
pause
endlocal
