# SNLA PyInstaller build script — PyWebView Desktop Edition
# Run: pyinstaller snla.spec
# Output: dist/StatsTalk.exe

# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('snla', 'snla'),                          # All source modules
        ('.env.example', '.'),                     # Config template
        ('data/fixtures/test_data.sav', 'data/fixtures'),  # Sample data for Demo
    ],
    hiddenimports=[
        'flask', 'flask.json.provider', 'flask.sessions', 'flask.signals',
        'webview', 'pandas', 'pyreadstat', 'lxml',
        'dotenv', 'docx', 'numpy', 'requests', 'cryptography',
        'cryptography.hazmat.primitives.ciphers.aead', 'cryptography.hazmat.primitives.kdf.scrypt',
        'snla.config', 'snla.secrets', 'snla.session',
        'snla.data.reader', 'snla.data.sanitizer', 'snla.data.persistence', 'snla.data.range_expander',
        'snla.llm.client',
        'snla.llm.prompts.intent', 'snla.llm.prompts.method', 'snla.llm.prompts.syntax',
        'snla.syntax.validator', 'snla.syntax.templates',
        'snla.executor.spss', 'snla.executor.python', 'snla.executor.adapter',
        'snla.parser.output', 'snla.parser.schema', 'snla.parser._oms', 'snla.parser._lst',
        'snla.explainer.naturalize', 'snla.explainer.export', 'snla.explainer.charts',
        'snla.ui.server', 'snla.ui._helpers',
        'snla.ui.launch', 'snla.ui.security',
        'snla.analysis', 'snla.analysis.service', 'snla.analysis.applicability',
        'snla.orchestrator', 'snla.orchestrator.planner',
        'snla.trust', 'snla.mcp_server',
        'snla.rag', 'snla.rag.integration',
        'werkzeug', 'jinja2', 'markupsafe', 'itsdangerous', 'blinker', 'click',
        'scipy', 'scipy.stats', 'scipy.optimize',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'PIL',
        'chromadb', 'sentence_transformers', 'torch',
        'streamlit',
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
    name='StatsTalk',
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
    icon=None,
)
