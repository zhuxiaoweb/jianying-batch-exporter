# Windows 后台引擎

程序通过此目录的辅助程序调用固定的 Windows 剪映引擎 `11.6.0.14552`。首次启动会在选定的本地目录自动准备运行时。已有相同且校验通过的运行时可重复使用。手动准备方法见 [开发说明](../../docs/development.md)。

`bin/profile.json` 记录项目内附两份原生辅助程序的 SHA256，并记录用于效果蒙版核验的官方资源指纹。每次调用先核对辅助程序和运行时指纹。剪映专业版的日常安装、媒体工具和这里使用的后台引擎是独立路径。

`jy-draftc.exe` 的源代码和上游许可证见 [native/jy-draftc](../../native/jy-draftc/LICENSE)。`model-roundtrip.exe` 的源代码见 [model_roundtrip.cpp](../../native/model_roundtrip.cpp)。
