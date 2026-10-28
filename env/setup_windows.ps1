# Rebuild the afsim_agent conda environment on Windows.
# Run from the project root in "Anaconda PowerShell Prompt":
#   powershell -ExecutionPolicy Bypass -File env\setup_windows.ps1
$ErrorActionPreference = "Stop"

if (-not (conda env list | Select-String -Pattern "^afsim_agent\s" -Quiet)) {
    conda create -n afsim_agent python=3.12.15 pip -y
}
conda activate afsim_agent

python -m pip install --upgrade pip
pip install -r env\requirements-lock.txt
pip install -e . --no-deps

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - fill in DASHSCOPE_API_KEY and FALCON_CHAT_MODEL."
}

pytest tests\unit\orchestrator -q
