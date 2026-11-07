#!/bin/bash
#SBATCH --partition=med_24h_4gpu
#SBATCH --nodes=1
#SBATCH --gpus=4
#SBATCH --exclusive
#SBATCH --time=24:00:00
#SBATCH --job-name=llm_dataset
#SBATCH --output=logs/llm_dataset.log

echo "Starting job on 4 GPUs..."
mkdir -p logs

# Activate conda environment for Python 3.10
source /data2/home/ritujmanware/miniconda3/etc/profile.d/conda.sh
conda activate llm_env
set -a; source .env; set +a

# Install vLLM and pandas if not already installed
pip install vllm pandas > /dev/null 2>&1

# Force HuggingFace to download the massive model into the RAM Disk!
export HF_HOME="/dev/shm/huggingface_cache"
mkdir -p $HF_HOME

# Prevent CUDA memory fragmentation which causes OOMs on 40GB GPUs
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# Disable custom all-reduce to prevent 'invalid argument' CUDA errors when using discontinuous GPUs
export VLLM_CUSTOM_ALL_REDUCE=0

echo "Waiting for 4 GPUs to have at least 32GB of free memory..."
python3 -c '
import subprocess, time, sys
print("Polling nvidia-smi every 15 seconds...", flush=True)
while True:
    try:
        smi = subprocess.check_output(["nvidia-smi", "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"]).decode("utf-8")
        free_gpus = []
        for line in smi.strip().split("\n"):
            idx, free = line.split(",")
            if int(free) > 32000:
                free_gpus.append(idx.strip())
        if len(free_gpus) >= 4:
            selected = ",".join(free_gpus[:4])
            print(f"\nFound 4 free GPUs: {selected}", flush=True)
            with open("free_gpus.txt", "w") as f:
                f.write(selected)
            sys.exit(0)
    except Exception as e:
        pass
    time.sleep(15)
'
export CUDA_VISIBLE_DEVICES=$(cat free_gpus.txt)

echo "Cleaning up any zombie processes left by previous crashed jobs on this node..."
pkill -9 -u $USER -f python || true
pkill -9 -u $USER -f ray || true
pkill -9 -u $USER -f vllm || true
sleep 2

echo "Current GPU status before starting:"
nvidia-smi

echo "Downloading model to RAM Disk and starting generation..."
python3 scripts/dataset_generator.py

echo "Job complete! Deleting the 150GB model from RAM Disk to clean up..."
rm -rf /dev/shm/huggingface_cache
echo "Done!"

