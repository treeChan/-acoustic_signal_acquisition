# 0.1.0

- 增加正式版与预览版独立渠道的启动静默检查和“帮助 → 检查更新…”。
- 增加安全 Markdown 更新说明、后台下载、进度、速度、预计剩余时间和取消操作。
- 增加 Ed25519 签名清单、SHA-256 和安装包描述签名三层验证。
- 增加采集/录制中的安全停机屏障，确保 HDF5、WAV、JSON 和 manifest 完整关闭后才安装。
- 增加 macOS 独立原子替换助手和 Windows x64 Inno Setup 原地升级安装器。
- 增加跨平台构建、签名、公证、发布后资产验证工作流及本地 HTTP 集成测试。

发布前请根据 `docs/updater.md` 配置所有 GitHub Actions Secrets，并在真实 macOS/Windows 测试机完成人工安装验证。
