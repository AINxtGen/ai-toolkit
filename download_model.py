'''
Download FLUX model script
Could be run standalone with: modal run download_model.py
'''

import os
import modal
from dotenv import load_dotenv

# Load the .env file if it exists
load_dotenv()

FLUX_MODEL_VOLUME = modal.Volume.from_name(
    "flux-base-model",
    create_if_missing=True
)
FLUX_MODEL_PATH = "/root/FLUX.1-dev"

# Image for download app
download_image = (
    modal.Image.debian_slim()
    .pip_install(
        "python-dotenv",
        "hf_transfer",
        "huggingface_hub", 
        "requests>=2.31.0", 
        "tqdm"
    )
    .env({
        "HF_HUB_ENABLE_HF_TRANSFER": "1",
        "HF_TOKEN": os.getenv("HF_TOKEN")
    })
)

# Download app
app = modal.App(
    name="flux-model-download",
    image=download_image,
    volumes={FLUX_MODEL_PATH: FLUX_MODEL_VOLUME}
)

@app.function(
    timeout=2700,
    volumes={FLUX_MODEL_PATH: FLUX_MODEL_VOLUME}
)
def download_flux_model():
    import os
    from huggingface_hub import snapshot_download

    def check_model_storage(folder_path, required_gb=53):
        total_size = 0
        required_size = required_gb * 1024 * 1024 * 1024  # Convert GB to bytes
        
        for dirpath, dirnames, filenames in os.walk(folder_path):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                total_size += os.path.getsize(file_path)
        
        current_gb = total_size / (1024*1024*1024)
        print(f"Current folder size: {current_gb:.2f}GB")
        return total_size >= required_size

    print("Checking FLUX model...")
    os.makedirs(FLUX_MODEL_PATH, exist_ok=True)
    
    if check_model_storage(FLUX_MODEL_PATH):
        print("FLUX model already exists, skipping download")
        return True
        
    try:
        print("FLUX model not found. Starting download...")

        snapshot_download(
            repo_id="black-forest-labs/FLUX.1-dev",
            local_dir=FLUX_MODEL_PATH,
            use_auth_token=os.getenv("HF_TOKEN"),
        )
        
        FLUX_MODEL_VOLUME.commit()
        print("FLUX model downloaded successfully")
        return True
    except Exception as e:
        print(f"Error downloading FLUX model: {e}")
        return False

if __name__ == "__main__":
    download_flux_model.remote() 