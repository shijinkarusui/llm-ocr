# -*- mode: python ; coding: utf-8 -*-
# serve v1: engine-bridge sidecar for the Tauri UI (S9).
# Entry serve.py is stdlib-only; engine modules come from src/ via pathex.
# Datas: prompts/*.md + tests/cand_165.png (probe fixture), resolved at
# runtime through serve._res_file (sys._MEIPASS when frozen).
# Excludes 复用旧 Tkinter 打包的瘦身表（conda 巨型簇）。
#
# onedir, not onefile: the portable build ships this as a folder -- `serve.exe`
# plus the sibling `_internal/` -- inside llm-ocr-<ver>-portable\, so there is no
# self-extraction into %TEMP%\_MEI* at all.  The shell kills the sidecar with a
# job object (KILL_ON_JOB_CLOSE), which the onefile bootloader never survived
# long enough to clean up after: every quit stranded one ~90 MB directory.
# Output: dist/serve/serve.exe + dist/serve/_internal/ (PyInstaller 6.x layout).
PROJ = '.'
block_cipher = None
a = Analysis(
    ['serve.py'],
    pathex=[PROJ, PROJ + '/src'],
    binaries=[],
    datas=[
        (PROJ + '/prompts/ocr_system.md', 'prompts'),
        (PROJ + '/prompts/notation_spec.md', 'prompts'),
        (PROJ + '/tests/cand_165.png', 'tests'),
    ],
    hiddenimports=[
        'llm_client', 'batch_plan', 'book_id', 'ocr_page', 'config', 'check_notation',
        'postprocess', 'make_searchable', 'verify_searchable', 'render',
        'geom_extract', 'geom_align',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'functorch', 'torchgen', 'triton',
        'transformers', 'tokenizers', 'huggingface_hub', 'safetensors',
        'scipy', 'sympy', 'mpmath', 'pandas', 'sklearn', 'nltk',
        'gradio', 'gradio_client', 'openai', 'networkx', 'sqlalchemy',
        'openpyxl', 'matplotlib', 'mpl_toolkits', 'pylab', 'einops',
        'onnxruntime', 'cv2', 'uvicorn', 'fastapi', 'starlette', 'typer',
        'opentelemetry', 'aiohttp', 'httpx', 'fsspec', 'websockets', 'joblib',
        'jinja2', 'yaml', 'lxml', 'bs4', 'soupsieve', 'requests', 'tqdm',
        'click', 'markdown_it', 'contourpy', 'groovy', 'hf_gradio',
        'pytest', '_pytest', 'pluggy', 'py', 'setuptools', 'pkg_resources',
        'wheel', 'IPython', 'jupyter', 'notebook', 'tornado', 'zmq',
        'cryptography', 'grpc', 'google', 'pydantic', 'rich', 'watchfiles',
        'anyio', 'h11', 'httpcore', 'starlette', 'multipart', 'email_validator',
    ],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
# onedir: EXE() emits only the bootloader + the embedded PYZ/scripts, then
# COLLECT() lays the dependency tree out beside it.  `exclude_binaries=True`
# is what moves binaries/datas out of the exe and into the COLLECT step; the
# resulting `_internal/` must stay a *sibling* of serve.exe.
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='serve', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='serve',
)
