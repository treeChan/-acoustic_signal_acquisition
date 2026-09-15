# 安全在线更新

## 范围与不变量

更新功能适用于 PyInstaller 打包的 macOS Apple Silicon `.app` 和 Windows x64 Inno Setup 安装版。它不依赖 Tauri，也不在运行中的主进程内覆盖自身。

最重要的不变量是：更新器只读写系统临时目录、应用安装目录和每用户的更新状态文件，绝不访问或清理采集记录目录。默认数据目录 `~/Documents/AcousticVectorRecords` 以及 GUI 中用户另选的目录不属于安装事务。HDF5、WAV、JSON 和 `manifest.jsonl` 不放在应用包或 Windows `{app}` 目录内。

## 完整链路

1. `updater.qt_ui.CheckWorker` 在独立 `QThread` 中读取渠道清单。
2. HTTP 客户端读取系统代理，并允许 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及对应小写变量生效；连接、读取和无进展均有界。
3. 客户端拒绝非 HTTPS URL、URL 凭据、过多重定向、重复 JSON 字段、未知字段、无效平台、无效版本和超大清单。只有测试模式的 localhost 可以使用 HTTP。
4. 在解析并采用任何清单值前，使用内置 Ed25519 公钥验证 `manifest_signature`。
5. `packaging.version.Version` 比较当前版本和清单版本。正式与预览渠道不交叉。
6. 下载在后台线程中流式写入系统临时目录 `acoustic-update-*/*.part`，显示字节数、总大小、速度和 ETA。取消、错误或验证失败会删除整个临时目录。
7. 下载完成后依次检查实际大小、SHA-256，以及覆盖版本、平台、URL、大小和哈希的 Ed25519 安装包描述签名。通过后才把 `.part` 原子改名。
8. 如果采集线程仍在运行，主窗口要求用户选择“安全停止并更新”。采集线程停止 USB、关闭/flush HDF5 与 WAV、等待 PCM 转换线程、写完 JSON 与 manifest，最后发出 `finished`。只有该信号无关闭错误时才继续。
9. 主程序启动独立 `AcousticUpdater` 后退出。外部助手等待父进程消失，并再次计算 SHA-256。
10. macOS 助手安全解压 ZIP、拒绝路径或符号链接逃逸、校验 bundle id，并在暂存前后执行 `codesign --verify --deep --strict` 与 `spctl --assess`。随后在同一安装目录内进行备份、原子替换和失败回滚，再重启应用。
11. Windows 助手在主程序退出后运行已签名的 Inno Setup。固定 `AppId` 使升级继承已有安装位置；安装成功后由安装器重启应用。

macOS 更新状态写入 `~/Library/Caches/AcousticVectorAcquisition/updater-status.json`；Windows 写入 `%LOCALAPPDATA%\AcousticVectorAcquisition\updater-status.json`。状态中不记录 token、私钥、代理用户名/密码或带查询串的 URL。

## 签名格式

发布脚本使用 UTF-8 JSON，`sort_keys=True`、紧凑分隔符 `(',', ':')`、禁止 NaN。两个域分离负载如下：

- 清单：ASCII `acoustic-vector-update-manifest-v1`、一个 NUL 字节、去掉 `manifest_signature` 后的稳定序列化 JSON。这个签名同时覆盖两个安装包的 URL、大小、哈希和各自签名。
- 安装包描述：ASCII `acoustic-vector-update-artifact-v1`、一个 NUL 字节、稳定序列化的 `version`、`platform`、`url`、`size`、`sha256`。

`scripts/make_update_manifest.py` 计算文件大小与 SHA-256、生成每个平台的 `.sha256` 和 `.sig`，最后生成带 `manifest_signature` 的 `update.json`。私钥只从临时文件读取；CI 临时文件来自 GitHub Actions Secret `UPDATE_ED25519_PRIVATE_KEY`。

`updater/public_key.pem` 已配置为本项目的发布公钥。匹配的私钥保存在仓库之外，不得提交、复制到安装包或写入日志；CI 只通过 GitHub Actions Secret 注入。不要为了处理密钥错误而关闭签名验证。

## 清单地址与渠道

- 正式：`https://github.com/treeChan/-acoustic_signal_acquisition/releases/latest/download/update.json`
- 预览：`https://github.com/treeChan/-acoustic_signal_acquisition/releases/download/preview/update.json`

正式 GitHub Release 必须是非 draft、非 prerelease，GitHub 的 `/releases/latest` 才能发现。预览渠道使用固定滚动 tag `preview`，Release 必须标为 prerelease。

## 错误可诊断性

客户端分别使用错误码处理：无效 URL、HTTP 状态、重定向、连接超时、读取超时、30 秒无进展、代理、TLS、网络中断、JSON、schema、平台缺失、版本、渠道、公钥、清单/安装包签名、哈希、大小、权限、安装包类型、助手缺失和助手启动失败。手动检查会显示具体错误；启动静默检查不会打断采集，只把具体原因放入状态提示。

日志只记录错误码和概括文本。`safe_url()` 删除 URL 用户信息、查询参数和 fragment，避免代理认证或临时下载参数进入日志。

## 本地测试

```bash
python3 -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
python3 -m compileall -q acoustic_acquisition.py acoustic_gui.py updater scripts tests
```

测试中的 Ed25519 密钥每次临时生成且不落盘。本地 HTTP 服务器覆盖正常更新、无更新、302、404、超时、下载中断、取消、错误哈希、错误签名、缺少平台和代理变量。安装计划测试显式指定临时状态路径并调用 `test_mode`，不覆盖真实 `.app`、Windows 安装或任何记录目录。

## 本地 macOS 构建

以下命令只输出到仓库的忽略目录；不要把结果复制到 `/Applications` 来做测试：

```bash
python3 -m PyInstaller --noconfirm --clean --distpath helper-dist --workpath build/helper packaging/acoustic_updater_helper.spec
ACOUSTIC_HELPER_PATH="$PWD/helper-dist/AcousticUpdater" python3 -m PyInstaller --noconfirm --clean --distpath dist --workpath build/app packaging/acoustic_gui.spec
QT_QPA_PLATFORM=offscreen dist/AcousticVectorAcquisition.app/Contents/MacOS/AcousticVectorAcquisition --smoke-test
```

本地包没有发布证书时只能验证启动与内容结构，不能当成正式更新包。正式工作流要求 Developer ID 签名、公证和 stapling，然后才生成 ZIP。

## GitHub Actions Secrets

必须配置：

- `UPDATE_ED25519_PRIVATE_KEY`：与客户端公钥匹配的 Ed25519 PKCS#8 PEM；只存 Secret。
- `MACOS_CERTIFICATE_P12_BASE64`：Developer ID Application 证书及私钥的 P12 Base64。
- `MACOS_CERTIFICATE_PASSWORD`：P12 密码。
- `APPLE_SIGNING_IDENTITY`：例如 `Developer ID Application: ... (TEAMID)`。
- `APPLE_ID`、`APPLE_APP_SPECIFIC_PASSWORD`、`APPLE_TEAM_ID`：Apple 公证凭据。
- `WINDOWS_CERTIFICATE_PFX_BASE64`：Windows Authenticode 证书及私钥的 PFX Base64。
- `WINDOWS_CERTIFICATE_PASSWORD`：PFX 密码。

内置 `GITHUB_TOKEN` 由 Actions 提供，不写入源码。工作流构建和测试完成后才创建 Release；最后通过 GitHub API 验证 Release 状态、`update.json`、两平台安装包、`.sha256`、`.sig`、清单版本和所有下载 URL。任一步不满足都会失败。

## 人工发布前检查

- 用离线备份保存 Ed25519 私钥；确认仓库和构建日志中没有私钥。
- 在一台非采集用测试 Mac 上验证下载、退出、替换、重启及无写权限提示。
- 在干净 Windows x64 虚拟机验证首次安装、原地升级、UAC、进程退出与重启。
- 在两平台各进行一次“录制中更新”，确认最终 HDF5/WAV/JSON/manifest 可读且样本数一致。
- 确认 macOS `codesign`、`spctl`、notarization/staple 和 Windows Authenticode 均有效。
