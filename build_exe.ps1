# 打包 hanime-crawler.exe（WebView 窗口版：双击直接弹出应用界面）
# 用法：powershell -ExecutionPolicy Bypass -File build_exe.ps1
# 产物统一在 dist\hanime-crawler\（中间 build/spec 收进 dist\_build，打包后自动删除）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$work = Join-Path $PSScriptRoot "dist\_build"
New-Item -ItemType Directory -Force -Path $work | Out-Null

python -m PyInstaller --noconfirm --clean --onedir --noconsole --name hanime-crawler `
  --workpath "$work" `
  --specpath "$work" `
  --add-data "$PSScriptRoot\app\static;app/static" `
  --collect-all webview `
  --collect-all pythonnet `
  --collect-all clr_loader `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import clr `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  --hidden-import anyio._backends._asyncio `
  launcher.py

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "打包完成: dist\hanime-crawler\"
