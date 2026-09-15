# 0.1.0

- 增加正式版与预览版独立渠道的启动静默检查和“帮助 → 检查更新…”。
- 增加安全 Markdown 更新说明、后台下载、进度、速度、预计剩余时间和取消操作。
- 增加 Ed25519 签名清单、SHA-256 和安装包描述签名三层验证。
- 增加采集/录制中的安全停机屏障，确保 HDF5、WAV、JSON 和 manifest 完整关闭后才安装。
- 增加 macOS 独立原子替换助手和 Windows x64 Inno Setup 原地升级安装器。
- 增加跨平台构建、签名、公证、发布后资产验证工作流及本地 HTTP 集成测试。
- 支持无 Apple/Windows 发行证书时生成名称明确的 unsigned 内部测试包；Ed25519 与 SHA-256 校验仍强制执行。
- 更新对话框会针对 macOS Gatekeeper 右键“打开”和 Windows SmartScreen/“未知发布者”显示明确提醒。

本版本当前按个人/实验室测试策略提供 unsigned 候选包。请仅从本仓库下载并核对 SHA-256；在真实 macOS/Windows 测试机完成人工安装验证后，再创建正式 tag。
