# Start the workbench on this machine: the agent (langgraph dev, 127.0.0.1:2024) and the
# frontend (deep-agents-ui, 127.0.0.1:3000). Copy to _local\workbench\start_workbench.ps1 and
# fill in the local paths there (that copy stays on this machine).
#
#   powershell -ExecutionPolicy Bypass -File _local\workbench\start_workbench.ps1
#   then open http://127.0.0.1:3000
param([switch]$Build)
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # _local\workbench → repo
$py = "<conda env>\python.exe"
$node = "<node dir>"                       # e.g. D:\tools\node-vXX
$logs = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$env:Path = "$node;$(Split-Path $py);$(Join-Path (Split-Path $py) 'Scripts');$env:Path"
$env:YARN_CACHE_FOLDER = "<cache dir>\yarn-cache"
$env:npm_config_cache = "<cache dir>\npm-cache"
$env:NEXT_TELEMETRY_DISABLED = "1"
# no traces of runs leave this machine
$env:LANGSMITH_TRACING = "false"
$env:LANGCHAIN_TRACING_V2 = "false"
# the orchestrator's model (generation and repair keep FALCON_CHAT_MODEL / the default)
$env:ORCHESTRATOR_MODEL = "qwen3-max"
# AFSIM for afsim_run / falcon_draft3 afsim=true (paths stay on the server side)
$env:FALCON_AFSIM_BINDING = "<binding .toml>"
$env:FALCON_AFSIM_KIT = "<scenario kit .toml>"
$env:AFSIM_MISSION = "<AFSIM bin>\mission.exe"
# extra replay roots for the simulation panel (';' separated), radar range drawn in replays
$env:WORKBENCH_REPLAY_ROOTS = "<local run dirs>"
$env:WORKBENCH_SENSOR_RANGE_M = "6000"

Push-Location $repo
Start-Process -FilePath (Join-Path (Split-Path $py) "Scripts\langgraph.exe") `
  -ArgumentList @("dev", "--host", "127.0.0.1", "--port", "2024", "--no-browser", "--allow-blocking") `
  -WorkingDirectory $repo -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\langgraph.out.log" -RedirectStandardError "$logs\langgraph.err.log"
Push-Location frontend
if ($Build -or -not (Test-Path ".next\BUILD_ID")) { & "$node\yarn.cmd" build }
Start-Process -FilePath "$node\yarn.cmd" -ArgumentList @("start") -WorkingDirectory (Get-Location).Path `
  -WindowStyle Hidden -RedirectStandardOutput "$logs\frontend.out.log" -RedirectStandardError "$logs\frontend.err.log"
Pop-Location
Pop-Location
"workbench: http://127.0.0.1:3000  (agent http://127.0.0.1:2024)"
