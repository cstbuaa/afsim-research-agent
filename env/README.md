# 运行环境

`requirements-lock.txt` 是 Mac 上 `afsim_agent` 环境（Python 3.12.15）的精确包版本，已去掉本机路径的可编辑安装和 Windows 上没有的 `uvloop`；`deepagents` 改为从 PyPI 安装同一版本 0.7.23。

## Windows

在 Anaconda PowerShell Prompt 中，于工程根目录运行：

```powershell
conda create -n afsim_agent python=3.12.15 pip -y
conda activate afsim_agent
pip install -r env\requirements-lock.txt
pip install -e . --no-deps
```

或运行 `powershell -ExecutionPolicy Bypass -File env\setup_windows.ps1`（同样的步骤，并在缺少时从 `.env.example` 生成 `.env`、最后跑一次测试）。

然后编辑 `.env`，填入 `DASHSCOPE_API_KEY`，并设置 `FALCON_CHAT_MODEL=qwen3.8-max`。

## Mac

已有环境无需重装。新装或升级包后，在工程根目录重新导出并提交：

```bash
conda activate afsim_agent
pip list --format=freeze > /tmp/freeze.txt
```

再按本文件开头的规则清理后覆盖 `env/requirements-lock.txt`。

## 说明

- 个别包在 Windows 上没有完全相同版本的预编译包时，`pip install` 会报错；把该行的版本号去掉再装即可。
- 网络有 HTTPS 拦截导致 pip 报证书错误时，可在 pip 命令后加 `--use-feature=truststore`（使用系统证书库）。
