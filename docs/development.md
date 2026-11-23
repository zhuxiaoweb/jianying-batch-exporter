# 开发与验证

主程序需要 Windows 10 x64、Python 3.12 x64（开发验证使用 3.12.14）和 Tkinter。开发机使用剪映专业版 `11.7.0.14652` 提供 FFmpeg 与 FFprobe，另在独立目录使用剪映 `11.6.0.14552` 作为后台引擎。

## 自动化测试

安装 [运行依赖](../requirements.txt) 后，在仓库根目录运行测试。

```powershell
.\.venv\Scripts\python.exe -B tools/run_current_tests.py
```

默认在 Windows 上运行当前源码、合成夹具和桌面 UI 测试。无桌面会话时加 `--headless`。其他系统可运行能跨平台执行的回归，完整导出需要 Windows。测试运行器把用户配置指向临时目录，可用 `--report <路径>` 输出带源码 SHA256 的 JSON。

在 Windows 上设置 `JY_TEST_APP_DIR` 指向同时包含 `ffmpeg.exe` 和 `ffprobe.exe` 的剪映安装版本目录，即可运行实际媒体测试。测试使用即时生成的小样。需要检查完整渲染链时，使用助手首次运行的环境自测，并以自有草稿进行真实导出和人工声画检查。

## 手动绑定后台引擎

正常首次启动会自动执行准备脚本。离线或已具备固定版本时，可从仓库根目录执行下面的命令，其中示例路径替换成自己的目录。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\headless-engine\Setup-HeadlessRuntime.ps1 `
  -ExistingRuntimeDirectory 'D:\JianyingExportRuntime\11.6.0.14552' `
  -ConfigurationPath (Join-Path $env:LOCALAPPDATA 'JianyingExportAssistant\engine_runtime.local.json')
```

脚本也支持传入 `-RuntimeRoot` 和 `-InstallerPath` 解压预先下载的官方安装包。脚本会校验 SHA256，再将引擎解压到指定目录。它不会运行剪映安装程序。把工作区和输出目录放到空间充足的位置。

## 本地构建 Windows EXE

在 Windows 上用 Python 3.12 运行以下命令。此步骤用于从当前源码生成本地便携目录。准备发布自己的改版时，需先在同一源码上完成回归，再重跑 Windows EXE 自测和真实草稿验证。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed --onedir --name jianying-batch-exporter main.py
$exporterEngine = '.\dist\jianying-batch-exporter\engine'
New-Item -ItemType Directory -Path $exporterEngine -Force | Out-Null
Copy-Item -Recurse .\tools\headless-engine\bin $exporterEngine
Copy-Item .\tools\headless-engine\Setup-HeadlessRuntime.ps1 $exporterEngine
Copy-Item LICENSE, THIRD_PARTY_NOTICES.md .\dist\jianying-batch-exporter
Copy-Item -Recurse .\licenses .\dist\jianying-batch-exporter
```

将辅助程序和环境脚本按以下目录结构放入便携目录。

```text
dist/jianying-batch-exporter/
├── jianying-batch-exporter.exe
├── _internal/
└── engine/
    ├── Setup-HeadlessRuntime.ps1
    └── bin/
        ├── jy-draftc.exe
        ├── model-roundtrip.exe
        ├── 7zr.exe
        ├── profile.json
        └── LICENSE.jy-draftc
```

启动便携版时保留整个目录。分发自行构建的便携版时，随包附上项目 `LICENSE`、`THIRD_PARTY_NOTICES.md` 和 `licenses/`。

### 原生辅助程序

日常开发可使用仓库中已核验的辅助程序。修改原生源码时，在 Windows 的 MSVC x64 开发者命令提示符构建。

```bat
native\jy-draftc\build.bat
if not exist native\jy-draftc\build mkdir native\jy-draftc\build
cl /nologo /std:c++20 /O2 /MT /EHsc /utf-8 /Fo:native\jy-draftc\build\model-roundtrip.obj /Fe:native\jy-draftc\build\model-roundtrip.exe native\model_roundtrip.cpp bcrypt.lib
```

替换本地 `tools/headless-engine/bin/` 中对应辅助程序后，将两份新文件的 SHA256 写入 `profile.json` 对应字段，再进行固定引擎和受控草稿验证。

## 修改导出逻辑

保留原稿只读、按精确片段确认配乐和字幕、四版规格与完整解码，以及断点续跑的文件核验。处理特效、缓存和子草稿兼容时，用合成样本记录复现和验证范围。公开 issue 优先提供版本、报错阶段和脱敏最小样本。
