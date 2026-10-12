# 打包版冒烟测试：离屏启动 dist\khQuantOS\khQuantOS.exe，检查日志里主窗口建成、没有异常。
# 用户目录全部指到临时目录，不碰本机真实的设置和数据。
param(
    [string]$Exe = "dist\khQuantOS\khQuantOS.exe",
    [int]$Seconds = 40
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $Exe)) { throw "找不到打包产物：$Exe" }
$root = Join-Path ([System.IO.Path]::GetTempPath()) ("khos_smoke_" + [guid]::NewGuid().ToString("N"))
foreach ($dir in @("home\Documents", "local", "roaming")) {
    New-Item -ItemType Directory -Force (Join-Path $root $dir) | Out-Null
}
$env:USERPROFILE = Join-Path $root "home"
$env:HOME = $env:USERPROFILE
$env:LOCALAPPDATA = Join-Path $root "local"
$env:APPDATA = Join-Path $root "roaming"
$env:QT_QPA_PLATFORM = "offscreen"
$env:KHQUANT_GUI_TEST_SKIP_DISCLAIMER = "1"

$process = Start-Process -FilePath (Resolve-Path $Exe) -PassThru
Start-Sleep -Seconds $Seconds
Get-Process -Name "khQuantOS" -ErrorAction SilentlyContinue | Stop-Process -Force

$log = Join-Path $env:LOCALAPPDATA "KhQuantOS\logs\app.log"
if (-not (Test-Path $log)) { throw "打包版没有写出日志：$log" }
$text = Get-Content $log -Raw -Encoding utf8
Get-Content $log -Encoding utf8 | Select-Object -Last 40
if ($text -notmatch "主窗口创建成功") { throw "打包版没有建出主窗口" }
if ($text -match "Traceback|ModuleNotFoundError|No module named") { throw "打包版日志里有异常" }
Write-Host "冒烟测试通过：打包版正常启动（运行 $Seconds 秒）"
