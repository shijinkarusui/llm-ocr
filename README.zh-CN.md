# llm-ocr

[English](README.md) · **简体中文**

> 扫描版中文语音学教材的 Vision-LLM OCR 工具链：逐页高精度 Markdown 转录
> （IPA 原样、附加符号 Unicode 组合正确）+ 原尺寸光栅垫底的双层可搜索 PDF。
> 桌面应用（Tauri 2 + React 19）经 `serve.py` 桥接调用同一套 `src/` 引擎，
> 网关只要求 OpenAI 兼容。API Key 只驻内存/环境变量，绝不进仓库。

---

## 0. 项目定位

- **输入**：扫描图片 / 扫描版 PDF（逐页栅格化为 PNG）。
- **产物 A**：高保真 Markdown —— 整本 `book.md` + 逐页 `pages/page_*.md`
  （中文标点归一、IPA 原样保留、附加符号以 Unicode 组合记号输出）。
- **产物 B**：双层可搜索 PDF `book_searchable.pdf` —— 原始光栅作背景
  （页面尺寸不变），叠加一层不可见的可搜索文字层。扫描书自带的乱码文字层
  **一律不复用**。
- **模型**：任意 OpenAI 兼容的 `POST /v1/*` 网关 —— `chat/completions`、
  `responses`、`messages` 三入站全开即可；任意官方字段经
  `**kwargs` / `--extra-json` 全透传。
- **Key 卫生**：`LLM_OCR_KEY`（亦支持 `LLM_OCR_API_KEY` / `OCTOPUS_API_KEY`），
  `.env` 自动加载，`.gitignore` 强制其永不进仓库。

核心设计取舍：

- **引擎与界面分离**：`src/` 不 import 任何 UI；前端经强类型 `lib/engine.ts`
- **配置双轨**：`GlobalConfig{base_url,model,key,timeout}` vs
  `RunConfig{endpoint,detail,concurrency,dpi,retries,system,extra}`，
  优先级 CLI > 环境变量 > 默认值；Key 只驻内存，`write_env()` 永不写明文，
  日志只打 `sk-**** + key_len`
- **全透传**：`generic_request + **kwargs / --extra-json`，任意官方字段直达
  网关 body，`model/stream` 除外；不写死 temperature / max_tokens
- **旧文字层绝不复用**：扫描书自带文字层多为乱码，一律丢弃，以原页光栅重建

## 1. 架构

```
                          +-------------------+
                          |   prompts/*.md    |  OCR 契约
                          | ocr_system.md     |  （转录规则、
                          | notation_spec.md  |   Unicode 组合映射）
                          +----+------+-------+
                               |      |
              +----------------+      +-----------------+
              |                                       |
     +--------v--------+                   +----------v--------+
     |   桌面应用       |  HTTP 桥接         |       CLI         |
     |  web/ (Tauri)   |<----------------->|  src/cli.py       |
     |  React 5 视图    |  serve.py :21139  |  models/probe/    |
     |  + 日志控制台    |  key 驻内存        |  ocr/batch/       |
     |  + 状态栏        |  job 轮询/取消     |  config/--tui     |
     +--------+--------+                   +----------+--------+
              |                                       |
              +-------------------+-------------------+
                                  |
                    +-------------v--------------+
                    |      核心引擎（src/）        |
                    |                            |
                    | config.py  双 dataclass     |
                    |  优先级 CLI > env > DEFAULT |
                    |  Key 只驻内存，永不写明文     |
                    |                            |
                    | llm_client.py  传输层：      |
                    |  generic_request 低层 +      |
                    |  三协议型别入口 + 视觉便捷    |
                    |  auto_vision() 1+1 回退      |
                    |  4xx fail-fast，429/5xx 重试 |
                    |  _resolve_endpoint 防 /v1/v1 |
                    +-------------+--------------+
                                  |
            +---------------------+----------------------+
            |                     |                      |
 +----------v---------+ +---------v--------+ +-----------v---------+
 | ocr_page.py        | | batch_plan.py    | | make_searchable.py  |
 | 单页：              | | 批量：            | | 双层 PDF：           |
 | 图片/PDF页/URL      | | 线程池 1-20       | | 光栅垫底（尺寸不变）  |
 | -> *_vision()      | | 自适应半减/回升    | | 文字层不可见          |
 | -> 页 Markdown     | | dry-run / 续跑    | | boxes 可靠走精确写入  |
 +----------+---------+ +---------+--------+ | 否则 fallback 双副本  |
            |                     |          +-----------v---------+
 +----------v---------+ +---------v--------+ +-----------v---------+
 | check_notation.py  | | postprocess.py   | | verify_searchable.py|
 | 门禁：禁 ASCII 替代 | | clean_md +       | | 关键词命中/可复制字数 |
 | 禁 LaTeX 残留       | | merge_pages +    | | compare_md 比对      |
 | 禁游离组合附加符     | | polish_with_llm  | |                     |
 +--------------------+ +------------------+ +---------------------+
```

### 层级职责

| 层 | 文件 | 负责 | 绝不触碰 |
|---|---|---|---|
| 契约 | `prompts/ocr_system.md`、`prompts/notation_spec.md` | 转录规则、Unicode 组合映射、IPA 保留清单 | 代码、密钥 |
| 配置 | `src/config.py` + `.env.example` | `GlobalConfig` vs `RunConfig`、优先级、dotenv 首句加载、密钥打码 | 传输、渲染 |
| 传输 | `src/llm_client.py` | 三入站、`**kwargs` 透传、`auto_vision()` 1+1、重试、endpoint 归一 | OCR 提示词、PDF 版面 |
| 单页/批量 | `src/ocr_page.py`、`src/batch_plan.py`、`src/book_id.py` | 单发、线程池 + 自适应并发、`usage.jsonl` 续跑、每书一目录 + 整本合并、`book.json` 身份绑定 | 网关内部 |
| 渲染/搜索 | `src/render.py`、`src/make_searchable.py` | 200dpi 光栅、光栅垫底 + 隐藏文字层、盒精确 vs 回退 | LLM 参数 |
| 质量 | `src/check_notation.py`、`src/postprocess.py`、`src/verify_searchable.py` | 标号门禁、CJK 清理 + 合并、关键词/可复制校验 | 密钥 |
| 界面 | `web/`（Tauri 2 + React 19）+ `serve.py` 桥接 | 5 视图、强类型 `lib/engine.ts`、托盘/单实例/原生对话框 | 引擎逻辑 |
| CLI | `src/cli.py`、`src/cli_common.py`、`src/interactive.py` | flag 单源（`add_llm_args`）、子命令、TUI shim | UI 组件 |

### 单页数据流

```
PDF 页 --render.py(200dpi)--> PNG bytes
  --> llm_client.*_vision(PNG, ocr_system.md, detail=high, **extra)
        chat:      messages[].content[{text},{image_url:{url:data|https,detail}}]
        responses: input[{role:user,content:[{input_text},{input_image}]}]（必须包一层 message）
        messages:  content[{text},{image:{source:{base64|url}}}]
  --> 页 Markdown
  --> check_notation.py 门禁（目标 0 issues）
  --> <输出目录>/<PDF 文件名去扩展名>/pages/page_NNNN.md
      （+ usage.jsonl 记 endpoint/extra/tokens）
  --> postprocess.clean_md + merge_pages（批量结束时）
      --> <输出目录>/<PDF 文件名去扩展名>/<同名>-ocr.md
  --> make_searchable(光栅 + md [+boxes]) --> book_searchable.pdf
  --> verify_searchable(keywords) --> 命中页 / 可复制字数
```

### 并发与续跑

- `batch_plan.py`：`ThreadPoolExecutor` 1–20（CLI 默认 4、库默认 1；>8 警告、
  >20 拒绝），信号量在 429/5xx 时半减并支持回升，跨进程文件锁，
  `usage.jsonl` 追加写 —— 重跑自动跳过 `status=success` 的页，`skipped` 也记行。
  日志与页文件都在「书名文件夹」`<输出目录>/<PDF 文件名去扩展名>/` 里
  （见 §6 第 3 条），分段续跑始终写进同一本书。
- 重试：按段计，4xx 立即失败（绝不重试），429/5xx/超时/`OSError` 重试；
  `auto` 模式 = responses 单试 → 仅在 429/5xx/超时/`OSError` 时回退 chat 单试
  （1+1，不会 9 倍爆炸）。

## 2. 快速开始

```bash
pip install -r requirements.txt        # python-dotenv + PyMuPDF
cp .env.example .env                   # 填入 LLM_OCR_KEY（.env 绝不提交）
python serve.py                        # 引擎桥接（:21139）
cd web && pnpm dev                     # 前端（:1420）
# 或联调：pnpm tauri dev（Rust 拉起 serve sidecar）
python -m src.cli models               # 列出网关模型
python -m src.cli probe --image tests/cand_165.png
```

单页 / 批量：

```bash
python src/ocr_page.py --image tests/cand_165.png --output out/cand_165.md
python src/ocr_page.py --image tests/cand_165.png --output out/a.md \
  --endpoint chat --detail high \
  --extra-json '{"temperature":0.2,"reasoning_effort":"low"}'
python src/batch_plan.py --dry-run --endpoint responses
python src/batch_plan.py --pdf book.pdf --output-dir out --endpoint chat
# -> out/book/pages/page_0000.md ... 以及整本 out/book/book-ocr.md
```

SDK 风格（绕过 CLI，同一套引擎）：

```python
from llm_client import generic_request
payload, usage = generic_request(
    {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
    endpoint="chat",   # chat | responses | messages（或完整 /v1/... 路径）
)
```

### 便携版

发行版以**便携文件夹**形式提供（`llm-ocr-<版本>-portable/`），**没有安装程序**。
解压后直接运行 `llm-ocr.exe` —— 引擎侧车与它的 Python 运行库就在旁边，因此
**目标机器无需安装 Python**，也不会往文件夹外写任何东西。打开「连接」视图，
填入网关地址、模型与 API Key 即可。

重新打包用 `pwsh -File build-portable.ps1`（见 §9）。

## 3. 目录结构

```
llm-ocr/
  serve.py              # 引擎桥接（stdlib HTTP，127.0.0.1:21139，job 制）
  serve.spec            # serve.exe 打包配置（PyInstaller onedir）
  web/                  # Tauri 2 + React 19 前端（见 spec-new-ui.md）
  src/                  # 引擎层
    config.py           # 中央配置（Global/Run 双 dataclass，dotenv 首句加载）
    llm_client.py       # 三协议传输核心
    ocr_page.py         # 单页 OCR（image / pdf-page / image-url）
    batch_plan.py       # 批量（线程池/自适应并发/dry-run/usage.jsonl/整本合并）
    render.py           # PyMuPDF 按页渲染 PNG（默认 200dpi）
    make_searchable.py  # 双层 PDF（光栅背景 + 隐藏文字层）
    postprocess.py      # clean_md（中文标点/页脚/公式保护）+ merge_pages
    book_id.py          # book.json 身份文件：写入/读取/按源 PDF 认书（无凭据）
    check_notation.py   # 标号门禁（禁 ASCII 替代、LaTeX 残留、未配对括号）
    verify_searchable.py# 成品验证（关键词命中页/可复制字数/compare_md）
    cli.py              # 统一入口 models/probe/ocr/batch/config/--tui
    cli_common.py       # flag 单源 add_llm_args（各入口共用）
    build_mineru_data.py# MinerU 对齐试点数据生成（输入目录见 §9）
  prompts/
    ocr_system.md       # OCR 系统提示词（转录 8 条 + 附加符号识读最高优先级）
    notation_spec.md    # Unicode 组合规范 + IPA 保留清单 + 校验要点
  tests/                # 样张 cand_165/166/167.png + page21_150.png 等
  requirements.txt      # python-dotenv + PyMuPDF（仅此两个运行时依赖）
  .env.example          # 可配 URL 契约文档（.env 本体 git-ignored）
  out/                  # 运行产物目录（git-ignored）
```

## 4. 配置体系（src/config.py）

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

## 5. 传输层（src/llm_client.py）

三层 API（由活到死）：

1. `generic_request(endpoint, body, **kw)` —— 你拼任意合法 JSON，
   只注入 model/base_url/key 后 POST；endpoint 可写别名或完整 `/v1/...`。
2. 型别入口 —— `chat_completions / responses_create / anthropic_messages`
   （均 `**kwargs` 直通 body）。
3. 视觉便捷 —— `chat_vision / responses_vision / anthropic_vision`
   （本地 PNG 先 base64 直传）及 `*_vision_url`（远端 https 由网关拉取，
   免 base64）；`detail: high|low|auto`（IPA 小字推荐 high）。
4. `auto_vision()` —— responses 单试，仅 429/5xx/timeout/OSError 回退 chat
   单试（1+1）；400/401/403/404 直接抛。

 可靠性：`HttpError` 区分可重试与 fail-fast；POST 超时 120s（`--timeout` 可调），
`GET /v1/models` 固定 15s 单试；`Retry-After` 上限 60s；
`_resolve_endpoint()` 防 `/v1/v1` 双写，支持 bare host:port 自动补
`http://` 与 https-443 判定。

## 6. OCR 链路（单页 / 批量 / 后处理 / 双层 / 验证）

1. **渲染**（`render.py`）：`render_page(pdf, pno_0based, dpi=200) -> PNG
   bytes`；推荐 200dpi（IPA 页约 1184x1788，b64 约 1.1MB；300dpi 约 2.4MB
   易超限）；参数强校验。
2. **单页**（`ocr_page.py`）：三种来源 image / pdf-page / image-url；
   `--endpoint/--detail/--extra-json` 活参；输出页 Markdown。
3. **批量**（`batch_plan.py`）：线程池 + 自适应并发、`--dry-run` 先看页数
   规划、`usage.jsonl` 追加写（断点续跑：success 自动跳过，skipped 也记行）、
   起止页范围、失败重试 2 次。**`--output-dir` 下按书分文件夹**（名字取 PDF
   文件名去扩展名），多本书不会混在一起：

   ```
   <输出目录>/语音学教程/
   ├── book.json             身份文件：源 PDF 绝对路径 + 大小 + 页数 +
   │                        pages_done + artifacts（绝不含凭据）
   ├── 语音学教程-ocr.md     整本 Markdown：run_batch 结束时用
   │                        postprocess.merge_pages 合并 pages/ 下全部页
   ├── pages/page_0000.md ... 逐页文件（文件名页码从 0 起）
   └── usage.jsonl           断点续跑记录（status=success 的页自动跳过）
   ```

   `book.json` 回答「这是哪本书的第 0 页」：它把文件夹绑定到某一本 PDF
   （绝对路径、大小、mtime、页数、已落盘页号、产物名），后续步骤据此**校验**
   而不是猜。同一本书重复跑是**幂等更新**（`created` 与自定义字段保留，
   `updated` / `pages_done` 刷新）；键名或值一旦长得像凭据，读写两侧都会被剔除。

   合并**不是纯拼接**：`merge_pages` 内部对每页再跑一次 `clean_md`，中文标点
   归一就在这一步做；空页写成 `[EMPTY PAGE]`。合并对象是**磁盘上实际存在的
   全部页文件**（不只是本次页码范围），所以分段跑多次会自然累积成整本。
4. **标号门禁**（`check_notation.py`）：`prompts/notation_spec.md` 的机器化身 —
   禁 `[t_w]`/`[tw]`/`[kh]` 类 ASCII 替代、禁 LaTeX 残留、禁拆分或游离的
   组合附加符、未配对括号。门禁目标 0 issues。
5. **后处理**（`postprocess.py`）：源码 ASCII-only，中文标点由转义码点构造；
   页脚 `· n ·` 单独成行；代码围栏与 LaTeX 先保护后恢复；
   `merge_pages(pages, title)` 把 `{页码: markdown}` 变成 `# <title>` +
   `## Page Index` + 每页一段 `<!-- PAGE n -->`（每页再走一遍 `clean_md`，
   空页写 `[EMPTY PAGE]`）；`batch_plan.run_batch` 在每次批量结束时以 PDF
   文件名去扩展名为 `title` 调用它，写出
   `<输出目录>/<书名>/<书名>-ocr.md`。
6. **双层**（`make_searchable.py`）：原页光栅做全页背景（尺寸不变，
   `maxdiff=0` 无二次压缩）；boxes 可靠（`reliable≠false`）走
   `render_mode=3` 按盒精确写入，否则 fallback 双副本
   （0.5pt 全文隐藏副本 + 按行均匀 8pt 分布），保证可搜可复制。
   构建前先由桥接层认书（`src/book_id.py`）：填**书文件夹**就按它自己的
   `book.json` 绑定并自动填出源 PDF（不必再选一次）；填**上级目录**则要求
   所选 PDF 唯一匹配其中一本（路径相同，或文件被搬走后大小+页数一致）；
   匹配不上或命中多本一律**明确报错并列出候选**，绝不静默挑一本。没有
   `book.json` 的旧目录照旧可用，并在首次构建成功后补写身份文件。双层 PDF
   落在书文件夹里，文件名随后记进 `artifacts.searchable_pdf`。
7. **验证**（`verify_searchable.py`）：逐页关键词命中 + 可复制总字数；
   `compare_md` 用 difflib 相似度比对两份 Markdown。

## 7. 桌面应用（Tauri 2 + React 19 + serve.py 桥接）

- 架构：Tauri 外壳（托盘/单实例/窗口记忆/原生对话框）+ React 前端
  （连接/单页/批量/双层/参数 5 视图 + 日志控制台 + 状态栏）+ `serve.py`
  引擎桥接（stdlib `http.server`，`127.0.0.1:21139`，job 制长任务）。
- Key 只驻前端内存，经请求 body/header 进桥接；日志只打 `sk-**** len=N`。
- 开发：`python serve.py` + `cd web && pnpm dev`；联调：`pnpm tauri dev`。
- 契约：`tests/test_serve_contract.py`（health/prompt/dry-run/notation 全离线）。
  详见 `spec-new-ui.md` §3 API 契约表。
- **生产构建必须用 `pnpm tauri build`。** 裸 `cargo build --release` 不带
  `tauri/custom-protocol` feature，产出的是 dev 形态（加载 `localhost:1420`、
  前端资源不嵌入）。快速判据：生产 `web.exe` 里含 `theme-init.js`。

## 8. CLI（src/cli.py + cli_common.py）

`python -m src.cli <models|probe|ocr|batch|config> [--tui]`；
`add_llm_args()` 是 flag 单源（`--base-url/--model/--api-key/--key-stdin/
--endpoint/--detail/--extra-json/--timeout/--system`，批量再加
`--concurrency`）；`--key-stdin` 三态；`--api-key` 明文传参会 warn。

## 9. 打包

发行版**只提供便携版**；`bundle.targets` 设为 `[]`，因此 Tauri 不再产出
NSIS/MSI。`build-portable.ps1` 跑完整条链，是唯一的出包方式：

```
PyInstaller onedir  ->  刷新 externalBin  ->  pnpm tauri build --no-bundle
                    ->  侧车冒烟测试        ->  组装  ->  打 zip
```

`serve.spec`（PyInstaller **onedir**，`console=False`）：`pathex` 覆盖根/src；
`datas` 带 `prompts/*.md` + `tests/cand_165.png`（冻结经 `sys._MEIPASS` 由
`serve._res_file` 解析）；`hiddenimports` 列全引擎模块 —— **包括 `book_id`**，
它在 `serve.py` 里是函数内延迟导入，虽然静态模块图通常能找到，仍需显式列出。

产出结构：

```
llm-ocr-<版本>-portable/
├── llm-ocr.exe      Tauri 外壳（前端资源已内嵌进 exe）
├── serve.exe        引擎侧车
└── _internal/       Python 运行库与支撑文件
```

`.gitignore` 已忽略 `dist-portable/`。

**为什么用 onedir 而不是 onefile。** onefile 侧车每次启动都会把自己解包到
`%TEMP%\_MEI*`（约 90MB）。而外壳是用作业对象终止侧车的，引导进程来不及
自清理，于是**每次运行都泄漏一个目录** —— 曾有一台机器累积到 116 个 / 9.96GB。
改 onedir 后不再解包，泄漏从根上消失。外壳仍会在启动与退出时清扫陈旧的
`%TEMP%\_MEI*`，但那只是替旧版本收尾；它只碰带本项目专属标记文件的目录，
其他 PyInstaller 程序的目录永不进入候选集。

**绝不要用裸 `cargo build --release` 出包。** 它不带 `tauri/custom-protocol`
feature，产出的是 dev 形态：加载 `localhost:1420`、前端资源不嵌入。
`pnpm tauri build`（带不带 `--no-bundle` 都一样）会自动带上。快速判据：
生产版 `llm-ocr.exe` 里含字符串 `theme-init.js`。

改用 onedir 后作业对象仍然重要：侧车现在是单进程，但 `kill_sidecar()` 只在
`RunEvent::ExitRequested` 触发，外壳被强杀时它根本不执行，引擎会继续占着
21139 —— `KILL_ON_JOB_CLOSE` 兜住的正是这种情况。

`src/build_mineru_data.py` 需要 MinerU 输出目录，默认取仓库同级的
`mineru-results/`，可用环境变量 `LLM_OCR_MINERU_DIR` 覆盖（该目录体积大，
不随仓库分发）。

## 10. 安全与限制

- Key 永不进仓库（`.gitignore`: `.env / out/ / __pycache__ / dist/ /
  build*`）；`--extra-json` 不要塞密钥类字段。
- `out/` 为本地运行产物（每本书一个子目录 `out/<书名>/`：pages/、
  usage.jsonl、`<书名>-ocr.md`、双层 PDF），不随仓库发布。
- `tests/` 样张仅为调试用小图；整书 PDF 请自行准备（`OCR_PDF_PATH` 或
  `--pdf` 指定）。
- `src/conn_test.py` 为占位脚本，无业务逻辑。

## 11. 更新日志

### 0.7.0 —— 双层 PDF 架构演进与体验全链路升级

- **双层 PDF 自动生成书签大纲（TOC Tree）。** 从识别出的 Markdown 标题结构（`#` ~ `######`）自动提取各级标题、层级深度及对应绝对物理页码，自动注入输出 PDF 的书签目录树中，电子书与教材导航体验质感飞跃。
- **逻辑页码映射与印号对齐（Page Labels）。** 双层 PDF 生成引擎能够检测页面实际印号并生成页码规则（`set_page_labels`），让阅读器中显示的页码与书籍印刷页码完全匹配，不再发生目录 10 页实际跳转物理 10 页的偏置问题。
- **公式与原子块拓扑保护。** 针对数学、化学公式块（`$$...$$`、`is_equation`），禁用拆行切碎逻辑（`_wrap_line`），采用等比自适应字号缩放并保留公式原子整体，彻底杜绝公式折行产生的字符重叠与乱序。
- **大书（数百至千页）全链路长效稳定性。** 全面加固几何门控、标点清洗与批处理容错机制，前端增加清晰进度与指标反馈，大幅提升批量处理与双层构建的长效稳定度。

### 0.6.3 —— 每书独立目录、身份绑定、便携版发行

- **每本书一个独立文件夹。** 批量产出落在 `<输出目录>/<PDF 文件名>/`，内含
  `pages/`、`usage.jsonl`、整本合并的 `<PDF 文件名>-ocr.md` 与双层 PDF。
  两本书不会再混淆 —— 这才是真正的失效模式：一个孤立的 `page_0000.md`
  无法自证它属于哪本书。
- **`book.json` 把 OCR 成果与源 PDF 绑定。** 由批量任务写入（源文件绝对路径、
  页数、大小、mtime、已完成页、产物清单），并且**刻意不记录 base_url，也不记录
  任何凭据**。双层那一步读取它：指向书文件夹就自动填出源 PDF；指向父目录就拿
  你选的 PDF 去匹配下面的书。**认不出或对不上时明确报错并列出候选**，绝不默默
  用错书。本版本之前的旧产出目录仍可用，并在首次双层构建成功后自动补上
  `book.json`。
- **整本 Markdown 真的会产出了。** `postprocess.merge_pages` 一直存在却从未被
  任何地方调用；现在批量会把磁盘上实际存在的全部页合并成
  `<PDF 文件名>-ocr.md`，因此分多次续跑也能累积成完整一本。两版 README 也
  改成了与代码一致的说法，不再是那条从未接线的数据流。
- **改为便携版发行，不再产安装包。** 以便携文件夹 + zip 取代 NSIS/MSI，见 §9。
- **侧车改用 onedir**，彻底消除每次运行对 `%TEMP%` 的解包 —— 见 §9。
- 批量任务完成时会显示整本 Markdown 的路径；双层页的「验证关键词」默认留空，
  并说明了这些关键词的实际作用。

### 0.6.2 —— 引擎连通性、生命周期与安装包修复

本版另含：批量产出改为**每本书一个文件夹**（`<输出目录>/<书名>/`，内含
`pages/`、`usage.jsonl`、身份文件 `book.json` 与合并出的 `<书名>-ocr.md`）——
此前只逐页落盘，`merge_pages` 只写在文档里、从未被调用，所以跑完只有散页、
没有整本；两本书 OCR 到同一个目录时也无法分辨谁是谁。

以下问题**全部是在打包后的发行版上跑出来的**，`pnpm tauri dev` 一个都
复现不了：Vite 代理会掩盖地址解析问题，而侧车在开发模式下是手工启动的。

- **生产构建连不上引擎。** `ENGINE_BASE` 用
  `location.protocol.startsWith("http")` 判断「浏览器还是 Tauri」，但 Tauri 2
  在 Windows 上用 `http://tauri.localhost` 提供页面 —— protocol 同样是
  `http:`。于是所有 `/api/*` 请求打到应用自己的资源服务器，拿回 `index.html`
  （报 `Unexpected token '<'`）。现改用 `__TAURI_INTERNALS__` 探测。
  `pick.ts` 里同一处误判则导致原生文件对话框被绕过，选中的路径变成纯文件名。
- **僵尸侧车导致第二次启动不可用。** `kill_sidecar()` 只终止了 PyInstaller
  的引导进程，真实子进程存活并继续占着端口与锁；而此时它的 stdout 管道已
  关闭，于是对任何请求都返回 0 字节。应用内没有任何自救入口。现改为把侧车
  纳入 Job Object（`KILL_ON_JOB_CLOSE`），并给侧车日志加护栏，使其在脱离
  父进程后降级而不是静默。
- **锁处理。** 硬杀留下的锁过去会让下次启动永久失败；现在锁内记录持有者
  PID 与启动时间，只有确认持有者已消失才回收。回收是原子的（唯一临时名 +
  `os.replace` + 全程持有句柄），堵住了一处「输家可能删掉赢家刚建立的锁」
  的竞态。
- **端口冲突变得响亮。** Windows 上启用 `SO_EXCLUSIVEADDRUSE`；此前第二个
  进程可以静默绑定同一端口并截走连接。现在冲突会以退出码 3 与明确信息结束。
- **失败可见。** 侧车输出被保留，引擎起不来时具体原因（退出码 + stderr 末尾）
  会出现在应用内「运行日志」以及
  `%LOCALAPPDATA%\cn.lxm.llmocr\logs\sidecar.log`，而不是只给一句 15 秒超时。
- **诊断信息。** 非 JSON 响应现在会报出 HTTP 状态码、URL、content-type 与
  正文前缀，而不是一个裸的解析错误。CSP 补上了 `connect-src`（桥接与 Tauri IPC）。
- **安装界面为简体中文**（`nsis.languages = ["SimpChinese"]`，MSI `zh-CN`）。
- **`%TEMP%` 不再无限增长。** 每次运行过去都会泄漏侧车约 93MB 的解包目录；
  现在启动与退出会清扫带本项目标记、且已存在 90 秒以上的目录。
