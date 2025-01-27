@echo off
setlocal enabledelayedexpansion

REM Check for admin privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] This script is not running with administrator privileges.
    echo Some features may not work correctly.
    echo.
    echo Please run this script as administrator:
    echo 1. Right-click on this script
    echo 2. Select "Run as administrator"
    echo.
    echo Do you want to continue anyway? [Y/N]
    set /p CONTINUE_NOADMIN="> "
    if /i not "%CONTINUE_NOADMIN%"=="Y" (
        echo Setup cancelled. Please restart with administrator privileges.
        pause
        exit /b 1
    )
    echo.
)

echo === FLUX LoRA Training Setup Script ===
echo.
echo This script will help you set up the environment for training LoRA models with FLUX:
echo  - Install required software (Python 3.10, Git) if not already installed
echo  - Clone the ai-toolkit repository to C:\ai-toolkit (to avoid Windows path length issues)
echo  - Set up virtual environment and dependencies
echo  - Configure Modal and Hugging Face tokens
echo.
echo NOTE: The repository will be cloned to C:\ai-toolkit
echo This location is chosen to prevent Windows path length limitations.
echo.
echo IMPORTANT: Make sure you have:
echo - Installed Python 3.10 or higher
echo - Installed Git
echo - Registered accounts on Modal and Hugging Face
echo - Accepted FLUX.1-dev license on Hugging Face (if using it)
echo.
echo Do you want to continue? [Y/N]
set /p CONTINUE="> "
if /i not "%CONTINUE%"=="Y" (
    echo Setup cancelled by user.
    pause
    exit /b 0
)
echo.

REM Check and install Python if needed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Downloading Python 3.10...
    echo.
    
    REM Download Python 3.10 installer
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe
    
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to download Python installer.
        echo Please download and install Python 3.10 manually from: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    
    echo Installing Python 3.10...
    echo NOTE: Please ensure you check "Add Python to PATH" during installation
    python_installer.exe /quiet InstallAllUsers=1 PrependPath=1
    
    if %errorlevel% neq 0 (
        echo [ERROR] Python installation failed.
        del python_installer.exe
        pause
        exit /b 1
    )
    
    del python_installer.exe
    echo Python 3.10 installed successfully!
    echo.
    set NEED_RESTART=1
)

REM Check and install Git if needed
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Git not found. Downloading Git...
    echo.
    
    REM Download Git installer
    curl -L -o git_installer.exe https://github.com/git-for-windows/git/releases/download/v2.42.0.windows.2/Git-2.42.0.2-64-bit.exe
    
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to download Git installer.
        echo Please download and install Git manually from: https://git-scm.com/downloads
        pause
        exit /b 1
    )
    
    echo Installing Git...
    git_installer.exe /VERYSILENT /NORESTART
    
    if %errorlevel% neq 0 (
        echo [ERROR] Git installation failed.
        del git_installer.exe
        pause
        exit /b 1
    )
    
    del git_installer.exe
    echo Git installed successfully!
    echo.
    set NEED_RESTART=1
)

REM Check if restart is needed
if defined NEED_RESTART (
    echo ============================================================
    echo IMPORTANT:
    echo You need to restart this script for the changes to take effect.
    echo.
    echo 1. Close this window
    echo 2. Open a new Command Prompt or Terminal
    echo 3. Navigate back to this folder
    echo 4. Run this script again
    echo ============================================================
    pause
    exit /b 0
)


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

echo [1/6] Setting up Git configuration...
REM Enable long paths in Git
git config --system core.longpaths true
if %errorlevel% neq 0 (
    echo [WARNING] Could not enable Git long path support. Try running as Administrator.
)

echo [2/6] Checking/Cloning ai-toolkit repository...
if exist "C:\ai-toolkit\" (
    echo Found existing ai-toolkit folder in C:\, skipping clone...
    cd /d C:\ai-toolkit
) else (
    echo Cloning to C:\ai-toolkit to avoid path length issues...
    cd /d C:\
    git clone https://github.com/AINxtGen/ai-toolkit.git
    if %errorlevel% neq 0 (
        echo [ERROR] Could not clone repository.
        pause
        exit /b 1
    )
    cd ai-toolkit
)

echo [3/6] Updating submodules...
git submodule update --init --recursive

echo [4/6] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate

echo [5/6] Installing Modal...
pip install modal

echo [6/6] Installing required dependencies...
pip install python-dotenv huggingface_hub oyaml

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
call venv\Scripts\activate
echo === Next Steps ===
echo Required files to prepare:
echo 1. Configuration file:
echo    - Customize settings according to your needs in config/modal_train_lora_flux.yaml
echo 2. Environment file (.env):
echo    - Add your Hugging Face token
echo 3. Training data files
echo.
echo Press Enter when you have prepared all required files to begin training...
pause

:CHECK_FILES
REM Check .env file
if not exist ".env" (
    echo [ERROR] .env file not found!
    echo Please create .env file and add your Hugging Face token in format:
    echo HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    echo.
    echo Press Enter after you have created the .env file...
    pause
    goto CHECK_FILES
)

REM Check HF_TOKEN format in .env
findstr /r /c:"^HF_TOKEN=hf_" ".env" >nul
if %errorlevel% neq 0 (
    echo [ERROR] Invalid HF_TOKEN format in .env file!
    echo Token should be in format: HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    echo.
    echo Press Enter after you have fixed the token format...
    pause
    goto CHECK_FILES
)

REM Check config file
if not exist "config/modal_train_lora_flux.yaml" (
    echo [ERROR] Configuration file not found!
    echo Please create config/modal_train_lora_flux.yaml
    echo You can copy from templates in config/examples/modal/
    echo.
    echo Press Enter after you have created the config file...
    pause
    goto CHECK_FILES
)

echo All required files are present.
echo.
echo [1/2] Checking/Downloading FLUX model if needed (this may take a while)...

timeout /t 1 /nobreak >nul

modal run download_model.py || (
    echo [ERROR] Failed to download FLUX model
    pause
    exit /b 1
)
echo.


timeout /t 1 /nobreak >nul

echo [2/2] Starting training process...

timeout /t 1 /nobreak >nul

modal run --detach run_modal.py::main --config-file-list-str=/root/ai-toolkit/config/modal_train_lora_flux.yaml || (
    echo [ERROR] Failed to start training process
    pause
    exit /b 1
)
echo.
echo Training process has started!
echo You can monitor the training progress and logs at: https://modal.com/logs
echo.
pause
