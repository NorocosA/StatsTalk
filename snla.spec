# SNLA PyInstaller build script
# Run: pyinstaller snla.spec
# Output: dist/SNLA.exe

# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('snla', 'snla'),          # All source modules
        ('.env.example', '.'),     # Config template
    ],
    hiddenimports=[
        'streamlit', 'pandas', 'pyreadstat', 'lxml',
        'dotenv', 'docx', 'numpy', 'requests',
        'snla.config', 'snla.session',
        'snla.data.reader', 'snla.data.sanitizer',
        'snla.llm.client',
        'snla.llm.prompts.intent', 'snla.llm.prompts.method', 'snla.llm.prompts.syntax',
        'snla.syntax.validator', 'snla.syntax.templates',
        'snla.executor.spss',
        'snla.parser.output', 'snla.parser.schema',
        'snla.explainer.naturalize', 'snla.explainer.export',
        'snla.ui.streamlit_app',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'scipy', 'PIL',
        'chromadb', 'sentence_transformers', 'torch',
    ],
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
    name='SNLA',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # Show console for status messages
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
