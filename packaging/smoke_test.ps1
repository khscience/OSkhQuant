# 独立目录/INI 设置中启动；Windows 作业对象清理本次进程树。
param(
    [string]$Exe = "dist\khQuantOS\khQuantOS.exe",
    [ValidateRange(1, 300)][int]$Seconds = 40,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
& $Python (Join-Path $PSScriptRoot "smoke_runner.py") --exe $Exe --seconds $Seconds
if ($LASTEXITCODE -ne 0) { throw "打包版冒烟失败，详情见上方报告路径" }
