# -*- mode: python ; coding: utf-8 -*-
# llm-ocr-gui v2: clean-venv build. pathex covers src+gui so bundled modules resolve;
# excludes drop the conda giant cluster (torch/gradio/transformers/...) that bloated v1 to 372MB.
PROJ = '.'
block_cipher = None
a = Analysis(
    ['app_gui.py'],
    pathex=[PROJ, PROJ + '/src', PROJ + '/gui'],
    binaries=[],
    datas=[(PROJ + '/prompts/ocr_system.md', 'prompts'), (PROJ + '/tests/cand_165.png', 'tests')],
    hiddenimports=[
        'gui_core', 'tab_connect', 'tab_single', 'tab_batch', 'tab_searchable', 'tab_params',
        'llm_client', 'batch_plan', 'ocr_page', 'config', 'check_notation',
        'postprocess', 'make_searchable', 'verify_searchable', 'render',
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
    name='llm-ocr-gui', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
