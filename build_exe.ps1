# 打包 hanime-crawler.exe（WebView 窗口版：双击直接弹出应用界面）
# 用法：powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --noconfirm --clean --onefile --console --name hanime-crawler `
  --add-data "app/static;app/static" `
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

Write-Host "打包完成: dist\hanime-crawler.exe"
