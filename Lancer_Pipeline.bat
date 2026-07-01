@echo off
setlocal
cd /d "%~dp0"
title Pipeline CIAM MGEN v4.0

set "PYTHON=%~dp0python\python.exe"

if not exist "%PYTHON%" (
    color 0C
    echo.
    echo  ====================================================
    echo   ERREUR : Python portable introuvable
    echo   Le dossier python\ doit etre a cote de ce fichier.
    echo  ====================================================
    pause
    exit /b 1
)

echo  [OK] Demarrage de la plateforme...
echo       Le navigateur va s'ouvrir sur http://127.0.0.1:5000
echo       Ne fermez pas cette fenetre pendant l'execution.
echo.
"%PYTHON%" "%~dp0app.py"
if errorlevel 1 (
    echo.
    echo  Le pipeline s'est arrete. Consultez les messages ci-dessus.
    pause
)
exit /b 0
