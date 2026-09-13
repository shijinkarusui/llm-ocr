# llm-ocr

> Vision-LLM OCR toolchain for scanned Chinese phonetics textbooks: per-page
> Markdown transcription (IPA-safe) + raster-backed searchable dual-layer PDF,
> driven by any OpenAI-compatible gateway. Desktop app (Tauri 2 + React 19,
> engine sidecar over HTTP) and CLI share one engine. API key lives in
> memory/env only — never in git.

English overview first; full Chinese manual below (Chapters 1-9).

---

## 0. What it is

- **Input**: scanned images / scanned PDF (each page rasterized to PNG).
- **Output A**: high-fidelity Markdown — whole book `book.md` + per-page
  `pages/page_*.md` (Chinese punctuation normalized, IPA kept verbatim,
  diacritics emitted as Unicode combining marks).
- **Output B**: dual-layer searchable PDF `book_searchable.pdf` — original
  raster as background (same page size), plus an invisible searchable text
  layer. The old garbled text layer is never reused.
- **Model**: any OpenAI-compatible `POST /v1/*` gateway —
  `chat/completions`, `responses`, `messages` all supported, arbitrary
  official fields pass straight through (`**kwargs` / `--extra-json`).
- **Key hygiene**: `LLM_OCR_KEY` (also `LLM_OCR_API_KEY`/`OCTOPUS_API_KEY`),
  `.env` autoload, `Key never in git` (`.gitignore` enforces it).

## 1. Architecture

```
                          +-------------------+
                          |   prompts/*.md    |  OCR contract
                          | ocr_system.md     |  (transcription rules,
                          | notation_spec.md  |   Unicode combining map)
                          +----+------+-------+
                               |      |
              +----------------+      +-----------------+
              |                                       |
     +--------v--------+                   +----------v--------+
     |  DESKTOP APP    |  HTTP bridge      |       CLI         |
     |  web/ (Tauri)   |<----------------->|  src/cli.py       |
     |  React 5 views  |  serve.py :21139  |  models/probe/    |
     |  + log console  |  key in memory    |  ocr/batch/       |
     |  + status bar   |  job poll/cancel  |  config/--tui     |
     |                 |                   |                   |
     |                 |  thin dispatch:   |  cli.py -> per-   |
     |                 |  bridge -> engine |  module mains,    |
     |                 |                   |  one resolve path |
     +--------+--------+                   +----------+--------+
              |                                       |
              +-------------------+-------------------+
                                  |
                    +-------------v--------------+
                    |      CORE ENGINE (src/)    |
                    |                            |
                    | config.py  GlobalConfig{   |
                    |  base_url,model,key,       |
                    |  timeout} vs RunConfig{    |
                    |  endpoint,detail,          |
                    |  concurrency,dpi,retries,  |
                    |  system,extra}             |
                    |  Priority: CLI > env >     |
                    |  DEFAULT; key in memory    |
                    |  only, write_env() never   |
                    |  persists plaintext        |
                    |                            |
                    | llm_client.py  TRANSPORT:  |
                    |  generic_request(endpoint, |
                    |  body) low-level + typed   |
                    |  chat_completions /        |
                    |  responses_create /        |
                    |  anthropic_messages +      |
                    |  *_vision (+_url) helpers; |
                    |  auto_vision() 1+1 route;  |
                    |  HttpError fail-fast 4xx,  |
                    |  retry 429/5xx/timeout;    |
                    |  _resolve_endpoint() anti  |
                    |  /v1/v1 double-write       |
                    +-------------+--------------+
                                  |
            +---------------------+----------------------+
            |                     |                      |
 +----------v---------+ +---------v--------+ +-----------v---------+
 | ocr_page.py        | | batch_plan.py    | | make_searchable.py  |
 | SINGLE PAGE:       | | BATCH:           | | DUAL-LAYER PDF:     |
 | image / pdf-page / | | ThreadPool 1-20, | | raster = background |
 | image-url -> PNG   | | adaptive halve/  | | (same size), text   |
 | -> *_vision()      | | recover, dry-run | | layer invisible;    |
 | -> page MD         | | plan, usage.jsonl| | boxes reliable ->   |
 |                    | | append (resume), | | render_mode=3 exact |
 +----------+---------+ | skipped logged   | | else fallback dual  |
            |           +---------+--------+ | copy (full-hidden   |
            |                     |          | + per-line spread)   |
 +----------v---------+ +---------v--------+ +-----------v---------+
 | check_notation.py  | | postprocess.py   | | verify_searchable.py|
 | GATE: banned       | | clean_md (CJK    | | VERIFY: per-page    |
 | ascii/lost marks/  | | punct, footers,  | | keyword hits,       |
 | no LaTeX residue   | | protect math) +  | | copyable chars,     |
 | -> 0 issues gate   | | merge_pages +    | | compare_md diff     |
 |                    | | polish_with_llm  | |                     |
 +----------+---------+ +---------+--------+ +-----------+---------+
            |                     |                      |
            +---------------------+----------------------+
                                  |
                    +-------------v--------------+
                    | render.py (PyMuPDF/fitz) |
                    | render_page(pdf,pno,dpi) |
                    | -> opaque PNG bytes      |
                    +--------------------------+
```

### Layer responsibilities

| Layer | Files | Owns | Never touches |
|---|---|---|---|
| Contract | `prompts/ocr_system.md`, `prompts/notation_spec.md` | Transcription rules, Unicode combining map, IPA keep-list | Code, keys |
| Config | `src/config.py` + `.env.example` | `GlobalConfig` vs `RunConfig`, CLI>env>DEFAULT, dotenv first-import, key masking | Transport, rendering |
| Transport | `src/llm_client.py` | 3 ingresses, passthrough `**kwargs`, `auto_vision()` 1+1, retries, endpoint normalize | OCR prompts, PDF layout |
| Page/Batch | `src/ocr_page.py`, `src/batch_plan.py` | Single shot, thread pool + adaptive concurrency, `usage.jsonl` resume | Gateway internals |
| Render/Search | `src/render.py`, `src/make_searchable.py` | 200dpi raster, raster-bg + hidden text, box/exact vs fallback | LLM params |
| Quality | `src/check_notation.py`, `src/postprocess.py`, `src/verify_searchable.py` | Notation gate, CJK cleanup + merge, keyword/copyable verify | Secrets |
| UI | `web/` (Tauri 2 + React 19) + `serve.py` bridge | 5 views, typed `lib/engine.ts`, tray/single-instance/native dialogs | Engine logic |
| CLI | `src/cli.py`, `src/cli_common.py`, `src/interactive.py` | One flag source (`add_llm_args`), subcommands, TUI shim | UI widgets |

### Data flow (one page)

```
PDF page --render.py(200dpi)--> PNG bytes
  --> llm_client.*_vision(PNG, ocr_system.md, detail=high, **extra)
        chat:      messages[].content[{text},{image_url:{url:data|https,detail}}]
        responses: input[{role:user,content:[{input_text},{input_image}]}] (must wrap message)
        messages:  content[{text},{image:{source:{base64|url}}}]
  --> page Markdown
  --> check_notation.py gate (0 issues)
  --> pages/page_NNNN.md (+ usage.jsonl row: endpoint/extra/tokens)
  --> postprocess.clean_md + merge_pages --> book.md
  --> make_searchable(raster + md [+boxes]) --> book_searchable.pdf
  --> verify_searchable(keywords) --> hits / copyable chars
```

### Concurrency & resume

- `batch_plan.py`: `ThreadPoolExecutor` 1–20 (CLI default 4, lib 1; >8 warns,
  >20 rejects), semaphore adaptive halving on 429/5xx with recovery,
  cross-process file lock (`msvcrt`/`fcntl`), `usage.jsonl` append-only —
  reruns skip `status=success` pages automatically; `skipped` is logged too.
- Retries: per-leg, 4xx fail fast (never retried), 429/5xx/timeout/`OSError`
  retried; `auto` mode = responses single-try → chat single-try on
  429/5xx/timeout/`OSError` only (1+1, no 9x explosion).

## 2. Quickstart

```bash
pip install -r requirements.txt        # python-dotenv + PyMuPDF
cp .env.example .env                  # fill in LLM_OCR_KEY (never commit .env)
python serve.py                       # engine bridge (:21139)
cd web && pnpm dev                  # frontend (:1420)
# 或联调：pnpm tauri dev（Rust 拉起 serve sidecar）
python -m src.cli models              # list gateway models
python -m src.cli probe --image tests/cand_165.png
```

Single page / batch:

```bash
python src/ocr_page.py --image tests/cand_165.png --output out/cand_165.md
python src/ocr_page.py --image tests/cand_165.png --output out/a.md \
  --endpoint chat --detail high \
  --extra-json '{"temperature":0.2,"reasoning_effort":"low"}'
python src/batch_plan.py --dry-run --endpoint responses
python src/batch_plan.py --pdf book.pdf --output-dir out --endpoint chat
```

SDK-style (bypass CLI, same engine):

```python
from llm_client import generic_request
payload, usage = generic_request(
    {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
    endpoint="chat",   # chat | responses | messages (or full /v1/... path)
)
```

---

# llm-ocr 中文手册

## 1. 项目定位

扫描版中文语音学教材的 Vision-LLM OCR 工具链：逐页高精度 Markdown 转录
（IPA 原样、附加符号 Unicode 组合正确）+ 原尺寸光栅垫底的双层可搜索 PDF。
桌面应用（Tauri 2 + React 19）经 `serve.py` 桥接调用同一套 `src/` 引擎，
网关只要求 OpenAI 兼容（`chat / responses / messages` 三入站全开即可，
实测为 Octopus 网关；OpenAI / AxonHub 同协议可直接换）。

核心设计取舍：

- **引擎与界面分离**：`src/` 不 import 任何 UI；前端经强类型 `lib/engine.ts`
- **配置双轨**：`GlobalConfig{base_url,model,key,timeout}` vs
  `RunConfig{endpoint,detail,concurrency,dpi,retries,system,extra}`，
  优先级 CLI > 环境变量 > 默认值；Key 只驻内存，`write_env()` 永不写明文，
  日志只打 `sk-**** + key_len`。
- **全透传**：`generic_request + **kwargs / --extra-json`，任意官方字段直达
  网关 body，`model/stream` 除外；不写死 temperature / max_tokens。
- **旧文字层绝不复用**：扫描书自带文字层多为乱码，一律丢弃，以原页光栅重建。

## 2. 目录结构

```
llm-ocr/
  serve.py              # 引擎桥接（stdlib HTTP，127.0.0.1:21139，job 制）
  serve.spec            # serve.exe 打包配置（约 45MB onefile）
  web/                  # Tauri 2 + React 19 新前端（见 spec-new-ui.md）
  src/                  # 引擎层
    config.py           # 中央配置（Global/Run 双 dataclass，dotenv 首句加载）
    llm_client.py       # 三协议传输核心（982 行，见第 4 章）
    ocr_page.py         # 单页 OCR（image / pdf-page / image-url）
    batch_plan.py       # 批量（线程池/自适应并发/dry-run/usage.jsonl）
    render.py           # PyMuPDF 按页渲染 PNG（默认 200dpi）
    make_searchable.py  # 双层 PDF（光栅背景 + 隐藏文字层）
    postprocess.py      # clean_md（中文标点/页脚/公式保护）+ merge_pages
    check_notation.py   # 标号门禁（禁 ASCII 替代、LaTeX 残留、未配对括号）
    verify_searchable.py# 成品验证（关键词命中页/可复制字数/compare_md）
    cli.py              # 统一入口 models/probe/ocr/batch/config/--tui
    cli_common.py       # flag 单源 add_llm_args（各入口共用）
    interactive.py      # shim：转调 cli.main（解 import 环）
    conn_test.py        # 占位小脚本（print 123）
  prompts/
    ocr_system.md       # OCR 系统提示词（转录 8 条 + 附加符号识读最高优先级）
    notation_spec.md    # Unicode 组合规范 + IPA 保留清单 + 校验要点
  tests/                # 样张 cand_165/166/167.png + page21_150.png 等
  requirements.txt      # python-dotenv + PyMuPDF（仅此两个运行时依赖）
  .env.example          # 可配 URL 契约文档（.env 本体 git-ignored）
  out/                  # 运行产物目录（git-ignored；随仓库只留 gui_redesign 报告）
```

## 3. 配置体系（src/config.py）

- `GlobalConfig`：`base_url / model / key / timeout`（where to talk）。
- `RunConfig`：`endpoint / detail / concurrency / dpi / retries / system /
  extra`（how to talk）。
- 优先级：`CLI flag > 环境变量 > DEFAULT`；`dpi/retries` 仅 CLI（无 env
  回退）；`LLM_OCR_PORT` 已废弃（设了就 warn 并忽略，唯一旋钮是 base_url）。
- CLI 默认 `endpoint=responses` / 并发 4；库默认 `chat` / 并发 1；
  `ns.is_cli` 区分两者。
- Key 卫生：只从 `api_key / --key-stdin / LLM_OCR_KEY / LLM_OCR_API_KEY /
  OCTOPUS_API_KEY / OCTOPUS_KEY` 读；`check()` 只打印打码长度。

`.env.example` 即契约文档：`LLM_OCR_BASE_URL` 必须是 `/v1` 根
（不是 `/v1/chat/completions`），模型在 Octopus 上是分组名（`OC/` 前缀
必须保留，裸名 400），其余官方字段走 `--extra-json`。

## 4. 传输层（src/llm_client.py）

三层 API（由活到死）：

1. `generic_request(endpoint, body, **kw)` —— 你拼任意合法 JSON，
   只注入 model/base_url/key 后 POST；endpoint 可写别名或完整 `/v1/...`。
2. 型别入口 —— `chat_completions / responses_create / anthropic_messages`
  （均 `**kwargs` 直通 body）。
3. 视觉便捷 —— `chat_vision / responses_vision / anthropic_vision`
  （本地 PNG 先 base64 体感直传）及 `chat_vision_url /
   responses_vision_url / anthropic_vision_url`（远端 https 由网关拉取，
   免 base64）；`detail: high|low|auto`（IPA 小字推荐 high）。
4. `auto_vision()` —— responses 单试，仅 429/5xx/timeout/OSError 回退 chat
   单试（1+1）；400/401/403/404 直接抛。

可靠性：`HttpError` 区分可重试与 fail-fast；POST 超时 90s（`--timeout` 可调），
`GET /v1/models` 固定 15s 单试；`Retry-After` 上限 60s；
`_resolve_endpoint()` 防 `/v1/v1` 双写，支持 bare host:port 自动补
`http://` 与 https-443 判定。

## 5. OCR 链路（单页 / 批量 / 后处理 / 双层 / 验证）

1. **渲染**（`render.py`）：`render_page(pdf, pno_0based, dpi=200) -> PNG
   bytes`；推荐 200dpi（IPA 页约 1184x1788，b64 约 1.1MB；300dpi 约 2.4MB
   易超限）；参数强校验（类型/正数/越界）。
2. **单页**（`ocr_page.py`）：三种来源 image / pdf-page / image-url；
   `--endpoint/--detail/--extra-json` 活参；输出页 MD。
3. **批量**（`batch_plan.py`）：线程池 + 自适应并发（429/5xx 半减、成功回升）、
   `--dry-run` 先看页数规划、`usage.jsonl` 追加写（含 endpoint/extra/tokens，
   断点续跑：success 自动跳过，skipped 也记行）、起止页范围、失败重试 2 次。
4. **标号门禁**（`check_notation.py`）：`prompts/notation_spec.md` 的机器化身 —
   禁 `[t_w]`/`[tw]`/`[kh]` 类 ASCII 替代、禁 LaTeX 残留
   （`\underset`/`\overset`/`$` 定界符）、禁拆分或游离的组合附加符、未配对括号。
   门禁目标 0 issues。
5. **后处理**（`postprocess.py`）：源码 ASCII-only，中文标点由转义码点构造；
   页脚 `· n ·` 单独成行；代码围栏与 LaTeX 先保护后恢复；
   `merge_pages` 页间 `<!-- PAGE n -->` + 头部索引；
   `polish_with_llm` 同样三协议活透传。
6. **双层**（`make_searchable.py`）：原页光栅做全页背景（尺寸不变，
   `maxdiff=0` 无二次压缩）；boxes 可靠（`reliable≠false`）走
   `render_mode=3` 按盒精确写入，否则 fallback 双副本
   （0.5pt 全文隐藏副本 + 按行均匀 8pt 分布），保证可搜可复制。
7. **验证**（`verify_searchable.py`）：`verify(pdf, keywords)` 逐页关键词命中 +
   可复制总字数；`compare_md` 用 difflib 相似度比对两份 Markdown。

## 6. 桌面应用（Tauri 2 + React 19 + serve.py 桥接）

- 架构：Tauri 外壳（托盘/单实例/窗口记忆/原生对话框）+ React 前端
  （连接/单页/批量/双层/参数 5 视图 + 日志控制台 + 状态栏）+ `serve.py`
  引擎桥接（stdlib `http.server`，`127.0.0.1:21139`，job 制长任务）。
- Key 只驻前端内存，经请求 body/header 进桥接；日志只打 `sk-**** len=N`。
- 开发：`python serve.py`（桥接）+ `cd web && pnpm dev`（前端，:1420）；
  联调：`pnpm tauri dev`（Rust 拉起 serve sidecar，失败回退 python 直跑）。
- 契约：`tests/test_serve_contract.py`（health/prompt/dry-run/notation 全离线）。
  详见 `spec-new-ui.md` §3 API 契约表。
## 7. CLI（src/cli.py + cli_common.py）

`python -m src.cli <models|probe|ocr|batch|config> [--tui]`；
`add_llm_args()` 是 flag 单源（`--base-url/--model/--api-key/--key-stdin/
--endpoint/--detail/--extra-json/--timeout/--system`，批量再加
`--concurrency`）；`--key-stdin` 三态（api_key 优先 + warn / TTY 报错 /
管道全读）；`--api-key` 明文传参会 warn（推荐 env / 管道）。

## 8. 打包

`serve.spec`（PyInstaller onefile，console=False）：`pathex` 覆盖根/src；
`datas` 带 prompts/*.md + tests/cand_165.png（冻结经 `sys._MEIPASS` 由
`serve._res_file` 解析）；`hiddenimports` 列全引擎模块；`excludes` 沿用旧
瘦身表（约 45MB，serve.exe）。Tauri `externalBin: binaries/serve` 随包，
NSIS（currentUser）+ MSI 双产物：setup.exe 约 46MB。
## 9. 安全与限制

- Key 永不进仓库（`.gitignore`: `.env / out/ / __pycache__ / dist/ /
  build*`）；`--extra-json` 不要塞密钥类字段。
- `out/` 为本地运行产物（book/pages/usage.jsonl/双层 PDF），不随仓库发布。
- `tests/` 样张仅为调试用小图；整书 PDF 请自行准备（`OCR_PDF_PATH` 或
  `--pdf` 指定）。
- `src/conn_test.py` 为占位脚本，无业务逻辑。
