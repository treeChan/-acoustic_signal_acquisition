# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

root = Path(SPECPATH).parent
version = (root / "VERSION").read_text(encoding="utf-8").strip()
apple_version = version.split("-", 1)[0]
helper = Path(os.environ["ACOUSTIC_HELPER_PATH"]).resolve()
if not helper.is_file():
    raise SystemExit(f"Missing external updater helper: {helper}")

datas = [
    (str(root / "VERSION"), "."),
    (str(root / "updater" / "public_key.pem"), "updater"),
]
binaries = [(str(helper), ".")]
hiddenimports = ["serial.tools.list_ports_osx"] if sys.platform == "darwin" else ["serial.tools.list_ports_windows"]

a = Analysis(
    [str(root / "acoustic_gui.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pyqtgraph.opengl", "OpenGL", "scipy", "torch", "matplotlib", "IPython", "jupyter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AcousticVectorAcquisition",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        target_arch="arm64",
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AcousticVectorAcquisition")
    app = BUNDLE(
        coll,
        name="AcousticVectorAcquisition.app",
        bundle_identifier="com.chenshu.acousticvector",
        # Apple's two standard bundle version keys only accept dotted numeric
        # components. Keep the exact SemVer (including preview suffix) in the
        # custom key used by release verification and diagnostics.
        version=apple_version,
        info_plist={
            "CFBundleDisplayName": "声学矢量采集系统",
            "CFBundleShortVersionString": apple_version,
            "CFBundleVersion": apple_version,
            "AcousticFullVersion": version,
            "NSHighResolutionCapable": True,
        },
    )
else:
    version_file = os.environ.get("ACOUSTIC_WINDOWS_VERSION_FILE")
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AcousticVectorAcquisition",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        version=version_file,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="AcousticVectorAcquisition",
    )
