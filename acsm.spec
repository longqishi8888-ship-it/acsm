# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('textual')

datas = []
binaries = []

# textual bundles tree-sitter/highlights/*.scm query files that are read at
# runtime via Path.read_text(); collect_submodules alone misses these.
datas += collect_data_files('textual')

# tree-sitter core + all language packages shipped by textual[syntax].
# Each package contains a native _binding.abi3.so and queries/*.scm files.
_ts_packages = [
    'tree_sitter',
    'tree_sitter_bash',
    'tree_sitter_css',
    'tree_sitter_go',
    'tree_sitter_html',
    'tree_sitter_java',
    'tree_sitter_javascript',
    'tree_sitter_json',
    'tree_sitter_markdown',
    'tree_sitter_python',
    'tree_sitter_regex',
    'tree_sitter_rust',
    'tree_sitter_sql',
    'tree_sitter_toml',
    'tree_sitter_xml',
    'tree_sitter_yaml',
]
for pkg in _ts_packages:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ['tui_session_manager.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='acsm',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
