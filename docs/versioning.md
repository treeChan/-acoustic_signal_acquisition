# 版本与发布规范

## 唯一版本来源

根目录 `VERSION` 是唯一人工维护的版本号来源。运行时窗口、关于对话框、PyInstaller 资源、macOS `Info.plist`、Windows EXE 资源、Inno Setup 和 `update.json` 都从它派生。不要在 Python 或工作流中另写一个版本号。

正式版只能是 `x.y.z`，对应 tag `vX.Y.Z`。预览版只能是 `x.y.z-preview.MMDDHHMM`，对应固定 tag `preview`。

预览时间戳规则：月份去前导零，日、时、分固定两位。例如 9 月 5 日 08:07 是 `9050807`，版本可写为 `0.2.0-preview.9050807`；12 月 15 日 18:30 是 `0.2.0-preview.12151830`。这样点号后的 SemVer 纯数字标识符不会有前导零。

`updater.versioning` 会同时验证日历时间和格式，并使用 `packaging.version.Version` 比较，不进行字符串比较。生成预览号可以调用 `preview_version()`。

macOS 的 `CFBundleShortVersionString` 与 `CFBundleVersion` 只能保存点分数字，因此写入核心 `x.y.z`；自定义 `AcousticFullVersion` 保存完整版本并由 CI 校验。Windows 固定版本四元组使用 `x.y.z.0`，产品版本字符串保存完整版本。

## 正式发布

当前首次正式发布版本为 `0.1.0`。确认所有 Secrets 已配置、公钥已提交、`main` CI 通过后，准确命令为：

```bash
cd /Users/chenshu/acoustic_signal_acquisition
python3 scripts/sync_version.py
git status --short
git add VERSION updater scripts packaging installer docs .github README.md requirements.txt requirements-dev.txt acoustic_gui.py .gitignore
git commit -m "Add secure signed application updates"
git push origin main
git tag -a v0.1.0 -m "Acoustic Vector Acquisition 0.1.0"
git push origin v0.1.0
```

最后一条命令触发正式构建。工作流只接受 tag 与 `VERSION` 完全一致的提交，并拒绝覆盖已存在的正式 Release。正式 Release 由工作流创建为非 draft、非 prerelease，并设置 latest。

不要在未经明确确认时执行这些提交、推送、打 tag 或正式发布命令。

## 预览发布

先把 `VERSION` 改成符合规则且晚于当前预览号的值，更新 `docs/release-notes.md`，提交并推送 `main`。首次创建 `preview` tag 可使用普通 push；后续滚动会移动同名 tag，属于 force-push，必须先取得明确授权：

```bash
git tag -f preview
git push origin preview --force
```

工作流将预览 Release 标为 prerelease，并用新构建覆盖该滚动 Release 的 assets。正式与预览清单 URL 完全独立。

## 回滚与密钥事件

不要重写已发布正式 tag。发现问题时发布更高补丁版本。若 Ed25519 私钥可能泄露，应立即停止发布、移除受影响 Release assets，并发布带新公钥的完整客户端；密钥轮换不能仅靠已受信任链路静默完成，需要人工分发新的可信客户端。
