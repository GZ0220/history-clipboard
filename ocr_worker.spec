# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


block_cipher = None

hiddenimports = [
    "rapidocr",
    "rapidocr.main",
    "rapidocr.ch_ppocr_det",
    "rapidocr.ch_ppocr_cls",
    "rapidocr.ch_ppocr_rec",
    "rapidocr.inference_engine.base",
    "rapidocr.inference_engine.onnxruntime",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
]
datas = collect_data_files("rapidocr")
binaries = collect_dynamic_libs("onnxruntime")

a = Analysis(
    ["ocr_worker.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
        "numba",
        "llvmlite",
        "pytest",
        "pygments",
        "lxml",
        "IPython",
        "jupyter",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ocr_worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ocr_runtime",
)
