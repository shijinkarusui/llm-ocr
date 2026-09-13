# -*- mode: python ; coding: utf-8 -*-
# serve v1: engine-bridge sidecar for the Tauri UI (S9).
# Entry serve.py is stdlib-only; engine modules come from src/ via pathex.
# Datas: prompts/*.md + tests/cand_165.png (probe fixture), resolved at
# runtime through serve._res_file (sys._MEIPASS when frozen).
# Excludes 复用旧 Tkinter 打包的瘦身表（conda 巨型簇）-> ~45MB onefile。
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
        'llm_client', 'batch_plan', 'ocr_page', 'config', 'check_notation',
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
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='serve', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
