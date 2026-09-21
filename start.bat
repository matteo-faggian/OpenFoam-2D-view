@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title OpenFOAM 2D Web GUI - Avvio

REM ========================================================================
REM  start.bat — Avvio dell'app Flask con controllo dipendenze.
REM
REM  Verifica e (se mancante / disinstallato) sistema:
REM    1. Python 3 + pip
REM    2. Pacchetti Python (flask, gmsh, numpy, matplotlib)
REM    3. Installazione OpenFOAM v2312
REM    4. MS-MPI (mpiexec.exe)
REM    5. libPstream.dll (versione MS-MPI, non dummy)
REM
REM  Posiziona questo file in webapp/ e fai doppio click per avviare.
REM ========================================================================

cd /d "%~dp0"

echo.
echo ==================================================
echo   OpenFOAM 2D Web GUI - Avvio
echo ==================================================
echo.

REM ---------- 1. Python ---------------------------------------------------
echo [1/5] Controllo Python...
where python >nul 2>&1
if errorlevel 1 (
    echo   [ERRORE] Python non trovato nel PATH.
    echo   Scarica e installa Python 3.10+ da: https://www.python.org/downloads/
    echo   IMPORTANTE: durante l'installazione spunta "Add Python to PATH".
    start https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%V in ('python --version 2^>^&1') do set PYVER=%%V
echo   OK - Python !PYVER!

REM ---------- 2. Pacchetti Python ----------------------------------------
echo.
echo [2/5] Controllo pacchetti Python...
python -c "import flask, gmsh, numpy, matplotlib" 2>nul
if errorlevel 1 (
    echo   Alcuni pacchetti mancano. Installazione in corso...
    python -m pip install --upgrade pip --quiet
    if exist requirements.txt (
        python -m pip install -r requirements.txt --quiet
    ) else (
        python -m pip install --quiet flask gmsh numpy matplotlib
    )
    if errorlevel 1 (
        echo   [ERRORE] Installazione pacchetti fallita.
        pause
        exit /b 1
    )
    echo   OK - Pacchetti installati
) else (
    echo   OK - Pacchetti gia' presenti
)

REM ---------- 3. OpenFOAM -------------------------------------------------
echo.
echo [3/5] Controllo OpenFOAM v2312...
set "OF_ROOT=I:\OPENFOAM\v2312"
set "OF_BIN=%OF_ROOT%\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2312\platforms\win64MingwDPInt32Opt\bin"
if not exist "%OF_BIN%\rhoCentralFoam.exe" (
    echo   [ERRORE] OpenFOAM non trovato in: %OF_ROOT%
    echo   Scarica e installa OpenFOAM v2312 da: https://www.openfoam.com/download/
    start https://www.openfoam.com/download/
    pause
    exit /b 1
)
echo   OK - OpenFOAM v2312 trovato

REM ---------- 4. MS-MPI ---------------------------------------------------
echo.
echo [4/5] Controllo MS-MPI (per esecuzione parallela)...
set "MPIEXEC=I:\OPENFOAM\bin\mpiexec.exe"
set "MPI_OK=0"
if exist "%MPIEXEC%" (
    set MPI_OK=1
    echo   OK - MS-MPI trovato in I:\OPENFOAM\bin
) else (
    REM Fallback: cerca nelle posizioni standard
    if exist "%ProgramFiles%\Microsoft MPI\Bin\mpiexec.exe" (
        set "MPIEXEC=%ProgramFiles%\Microsoft MPI\Bin\mpiexec.exe"
        set MPI_OK=1
        echo   OK - MS-MPI trovato in Program Files
    )
)
if "!MPI_OK!"=="0" (
    echo   MS-MPI non trovato. Tentativo di download automatico...
    set "MSMPI_INSTALLER=%TEMP%\msmpisetup.exe"
    REM URL ufficiale Microsoft per MS-MPI v10.1.3 ^(potrebbe cambiare^)
    curl -L --fail --silent --show-error -o "!MSMPI_INSTALLER!" ^
        "https://download.microsoft.com/download/a/5/2/a5207ca5-1203-491a-8fb8-906fd68ae623/msmpisetup.exe"
    if errorlevel 1 (
        echo   [AVVISO] Download fallito. Apro la pagina nel browser:
        start https://www.microsoft.com/en-us/download/details.aspx?id=105289
        echo   Scarica msmpisetup.exe, installalo, poi rilancia questo file.
        pause
        exit /b 1
    )
    echo   Download completato. Avvio installer ^(richiede privilegi admin^)...
    "!MSMPI_INSTALLER!"
    echo   Quando l'installazione e' completata, rilancia start.bat.
    pause
    exit /b 0
)

REM ---------- 5. libPstream.dll swap -------------------------------------
echo.
echo [5/5] Controllo libreria parallela (libPstream.dll)...
set "PSTREAM=%OF_BIN%\libPstream.dll"
set "PSTREAM_MPI=%OF_BIN%\libPstream.dll-msmpi"
if exist "%PSTREAM_MPI%" (
    REM La versione dummy e' 35'328 byte, la MS-MPI e' 220'672 byte
    for %%I in ("%PSTREAM%") do set "PSIZE=%%~zI"
    if "!PSIZE!"=="35328" (
        echo   libPstream.dll e' la versione dummy ^(seriale^). Swap a MS-MPI...
        copy /Y "%PSTREAM_MPI%" "%PSTREAM%" >nul
        if errorlevel 1 (
            echo   [AVVISO] Swap fallito. Esegui manualmente:
            echo     copy "%PSTREAM_MPI%" "%PSTREAM%"
        ) else (
            echo   OK - libPstream.dll ora supporta MPI
        )
    ) else (
        echo   OK - libPstream.dll gia' nella versione MS-MPI
    )
) else (
    echo   [AVVISO] %PSTREAM_MPI% non trovato.
    echo   L'esecuzione parallela potrebbe non funzionare.
)

REM ---------- Avvio app --------------------------------------------------
echo.
echo ==================================================
echo   Tutti i controlli OK. Avvio Flask...
echo ==================================================
echo.

REM Avvia Flask in una finestra separata cosi' che chiudere questa
REM finestra batch non uccida il server.
start "OpenFOAM 2D Web GUI - Server" python app.py

REM Aspetta che Flask sia pronto, poi apri il browser automaticamente.
echo   Attendo avvio server...
timeout /t 3 /nobreak >nul
start http://127.0.0.1:5000
echo.
echo   Il browser si e' aperto su http://127.0.0.1:5000
echo   Il server Flask gira nella finestra "OpenFOAM 2D Web GUI - Server".
echo   Per fermarlo, chiudi quella finestra o premi CTRL+C al suo interno.
echo.
endlocal
