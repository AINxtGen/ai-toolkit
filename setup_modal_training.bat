@echo off
setlocal enabledelayedexpansion

echo === FLUX LoRA Training Setup Script ===
echo.
echo IMPORTANT: Make sure you have:
echo - Python 3.10 or higher installed
echo - Git installed
echo - Registered accounts on Modal and Hugging Face
echo - Accepted FLUX.1-dev license on Hugging Face (if using it)
echo.
REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed. Please install Python 3.10 or higher.
    echo Download Python at: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Check if git is installed
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Git is not installed. Please install Git.
    echo Download Git at: https://git-scm.com/downloads
    pause
    exit /b 1
)

echo [1/6] Cloning ai-toolkit repository...
git clone https://github.com/AINxtGen/ai-toolkit.git
if %errorlevel% neq 0 (
    echo [ERROR] Could not clone repository.
    pause
    exit /b 1
)

cd ai-toolkit

echo [2/6] Updating submodules...
git submodule update --init --recursive

echo [3/6] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate

echo [4/6] Installing Modal...
pip install modal

echo [5/6] Installing required dependencies...
pip install python-dotenv huggingface_hub

echo [6/6] Setting up Modal...
echo ============================================================
echo How to set up Modal token:
echo 1. Go to https://modal.com/settings/tokens
echo 2. Click "New Token"
echo 3. Copy the command that looks like:
echo    modal token set --token-id ak-xxxx --token-secret as-xxxx
echo 4. Right-click in this window to paste the command then press Enter
echo ============================================================
echo.

:GET_TOKEN
set /p MODAL_CMD="Paste Modal token command: "

REM Check if the command format is correct
echo %MODAL_CMD% | findstr /r /c:"^modal token set --token-id .* --token-secret .*" >nul
if %errorlevel% neq 0 (
    echo [ERROR] Invalid token format. Command should look like:
    echo modal token set --token-id ak-xxxx --token-secret as-xxxx
    echo Please try again.
    echo.
    goto GET_TOKEN
)

echo.
echo Executing token command...
%MODAL_CMD%
if %errorlevel% neq 0 (
    echo [ERROR] Failed to set Modal token. Please try again.
    goto GET_TOKEN
)
echo Modal token set successfully!
echo.
echo === Setup Complete! ===
echo.
echo Next steps:
echo 1. Create a config file in config/ folder (you can copy from config/examples/modal/)
echo 2. Edit the config file according to your needs
echo 3. Set up Hugging Face token in .env file
echo 4. Run training with command: modal run --detach run_modal.py --config-file-list-str=/root/ai-toolkit/config/modal_train_lora_flux.yaml
echo.
pause 