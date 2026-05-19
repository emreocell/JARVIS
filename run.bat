@echo off
REM JARVIS — tek-tıkla çalıştırıcı.
setlocal
cd /d "%~dp0"
set PLAYWRIGHT_BROWSERS_PATH=runtime\ms-playwright

REM 1) venv yoksa mevcut Python ile oluştur.
if not exist ".venv\Scripts\python.exe" (
    echo [JARVIS] .venv bulunamadi, olusturuluyor...
    py -3.12 -m venv .venv 2>nul
    if errorlevel 1 (
        python -m venv .venv
        if errorlevel 1 (
            py -m venv .venv
            if errorlevel 1 (
                echo [JARVIS] Python bulunamadi. Lutfen Python 3.12 yukleyin.
                pause
                exit /b 1
            )
        )
    )
    echo [JARVIS] Bagimliliklar yukleniyor...
    .venv\Scripts\python.exe -m pip install --upgrade pip wheel setuptools --quiet
    .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo [JARVIS] Bagimlilik yuklemesi basarisiz.
        pause
        exit /b 1
    )
)

dir /b "runtime\ms-playwright\chromium-*" >nul 2>nul
if errorlevel 1 (
    echo [JARVIS] Playwright Chromium bulunamadi, yukleniyor...
    if not exist "runtime\ms-playwright" mkdir "runtime\ms-playwright"
    .venv\Scripts\python.exe -m playwright install chromium
    if errorlevel 1 (
        echo [JARVIS] Playwright Chromium yuklenemedi; JARVIS acilacak ama Playwright browser automation sinirli kalabilir.
    )
)

REM 2) JARVIS'i baslat.
echo [JARVIS] Baslatiliyor...
.venv\Scripts\python.exe main.py
endlocal
