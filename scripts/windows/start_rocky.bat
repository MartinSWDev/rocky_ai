@echo off
REM Rocky server launcher (PC / Windows).
REM Secrets come from .env in the repo root; config from config.yaml.
REM Edit REPO below if you clone somewhere other than C:\Repos\rocky_ai.

set REPO=C:\Repos\rocky_ai
cd /d "%REPO%"

REM Wait for Ollama to be reachable before starting (handles boot ordering).
:waitollama
curl -s -o NUL http://localhost:11434/api/tags
if errorlevel 1 (
    echo Waiting for Ollama...
    timeout /t 3 /nobreak >NUL
    goto waitollama
)

py "%REPO%\rocky_server.py"
