## fork from https://github.com/ostris/ai-toolkit with some adjustments to fix issues when training LoRA on Modal

# FLUX LoRA Training Setup Guide

This guide will help you set up the environment for training LoRA models with FLUX on Modal

## Prerequisites

Before starting, make sure you have:
- Administrator privileges on your Windows system (for Windows users)
- Registered accounts on [Modal](https://modal.com) and [Hugging Face](https://huggingface.co)
- Accepted FLUX.1-dev license on Hugging Face (if using it)

## Setup Instructions

### For Windows Users:

1. Run `setup_modal_training.bat` as Administrator
   - Right-click on the script
   - Select "Run as administrator"

### For MacOS Users:

1. Open Terminal and navigate to the project directory
2. Make the setup script executable:
   ```bash
   chmod +x setup_modal_training.sh
   ```
3. Run the setup script:
   ```bash
   ./setup_modal_training.sh
   ```

### Common Steps for Both Platforms:

1. Follow the Modal token setup:
   - Go to https://modal.com/settings/tokens
   - Click "New Token"
   - Copy the command that looks like:
     ```
     modal token set --token-id ak-xxxx --token-secret as-xxxx
     ```
   - Paste the command when prompted

2. Prepare required files:
   - Configuration file:
   - Customize settings according to your needs in config/file modal_train_lora_flux.yaml
   - Environment file (`.env`):
     - Add your Hugging Face token in format:
     ```
     HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
     ```

## Installation Process

The setup script will automatically:
1. Install required software (if not already installed):
   - Python 3.10 or higher
   - Git (on MacOS, you can install it via Homebrew if needed)

2. Clone the ai-toolkit repository:
   - Windows: to `C:\ai-toolkit` (to prevent path length limitations)
   - MacOS: to the current directory
   
3. Set up virtual environment and dependencies
4. Configure Modal and Hugging Face tokens

## Starting Training

Once all files are prepared, the training process will start automatically with:
```
modal run --detach run_modal.py --config-file-list-str=/root/ai-toolkit/config/modal_train_lora_flux.yaml
```

You can monitor the training progress and logs at: https://modal.com/logs

## Troubleshooting

If you encounter any issues:
1. Make sure you're running the script as Administrator
2. Check that all required tokens are correctly set up
3. Verify that Python and Git are properly installed and added to PATH
4. Ensure all required files are present and correctly formatted

## Note

If you need to restart the setup process:
1. Close the current window
2. Open a new Command Prompt or Terminal
3. Navigate back to the installation folder
4. Run the script again as Administrator
## Download the content by running
```
modal volume get flux-lora-models your-model-name
```