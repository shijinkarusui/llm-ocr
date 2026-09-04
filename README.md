# llm-ocr

> 位置：本仓库根目录
> 状态：已实现（2026-09-03 实测双链路 200 + 同日灵活化改造已落地，方案 C 三协议统一，`task_plan.md` 为执行主规划）
> 测试书：本地扫描版《语音学教程》（300+ 页，旧文字层乱码，测试时丢弃，未随仓库发布）

---

## 1. 任务目标

建一个独立文件夹工程，实现：

1. **输入**：扫描图像 / 扫描PDF（按页渲染为PNG）。
2. **输出A**：高精度Markdown（全书 `book.md` + 逐页 `out/pages/page_*.md`）。
   - 中文标点规范，段落标题保留，页脚 `· n ·` 单独成行。
   - IPA原样保留：`ɕ ŋ ə ɛ ɔ ɑ β ʔ ʂ tɕ tʂ ʰ ː ˈ ³⁵`等。
   - 附标位置语法100%正确，见第4章。
3. **输出B**：双层可搜索PDF `book_searchable.pdf`。
   - 原页光栅做背景（尺寸不变），新加不可见可搜索文字层。
   - 可复制、可检索、可定位到页。旧乱码层绝不复用。
4. **模型**：用户接口 `http://YOUR_GATEWAY_HOST:2113/v1` / `MODEL=OC/muse-spark-1.3-contributor-free` / `KEY` 走环境变量 `LLM_OCR_KEY`（兼容 `LLM_OCR_API_KEY`/`OCTOPUS_API_KEY`，不硬编码），**最终交付必须可自定义 URL**：`LLM_OCR_BASE_URL` / `--base-url` 覆盖默认，`LLM_OCR_MODEL` / `--model` 同理，`_resolve_endpoint()` 自动防 `/v1/v1` 双写，默认 `OC/` 前缀必须保留（裸名会 400）。
5. **灵活调用（新增）**：不再把 `max_tokens=100000 / temperature=0` 写死；任意官方 OpenAI / Responses / Anthropic 字段通过 `**kwargs` / `--extra-json` 直通网关，三条入站 `chat / responses / messages` 全开放（见第3章）。
6. **禁令**：全流程只允许“原PDF光栅 + LLM输出”，禁止读 `mineru-results`，禁止提MinerU。

已验证结论（2026-09-03 实测，不重测）：

- `POST /v1/chat/completions` 与 `POST /v1/responses` 均 `200`（`cand_165.png` 113KB, b64 151952：chat 19.1s in474/out1616，responses 7.3s in474/out615，证物 `out/probe_ocr_chat.json` / `out/probe_ocr_responses.json`），`GET/POST /v1/files` -> `404` 未实现，无需使用。
- 三条链路和官网 openai/anthropic 完全一致：`data:image/png;base64,...` 本地先 base64 体感直传 + `https://...` 远端免 base64 均支持，`detail: high|low|auto` 可选（IPA 推荐 high）。
- `responses` 的 `input` 必须包 `[{role:user, content:[input_text, input_image]}]`，顶层平铺会 400 `input[0] did not match any supported type`。
- page21（150dpi）约3356~4555 tokens；IPA重页cand166（原书146页）6处 `underset`、零`[t_w]`。
- 推荐DPI=200（IPA页1184x1788，b64约1.13MB<1.5MB，300dpi约2.4MB超限）。
- 303页估算约200万tokens。
- 旧层175/303页可疑，`strip_check=must_discard`。
- boxes“部分可用”（33行，中心误差<13px，标题差字，不能直写精确覆盖，要fallback）。
- 双层样例3页背景`maxdiff=0`，顺/逆同化各命中4次（fallback双副本导致重复，预期内）。

---

## 2. 实现流程

```
原PDF --render.py(200dpi)--> PNG
  --> llm_client.chat_vision(PNG, ocr_system.md)          // chat: image_url
      |_ llm_client.responses_vision(PNG, ...)           // responses: input_image
      |_ llm_client.anthropic_vision(PNG, ...)           // messages: base64
      |_ chat_vision_url / responses_vision_url / anthropic_vision_url (https 免 base64)
      +-- **kwargs / --extra-json 全透传 --> 页MD
  --> check_notation.py门禁 --> out/pages/
  --> postprocess.clean_md + merge_pages --> book.md
  --> make_searchable(背景PNG + MD [+boxes]) --> book_searchable.pdf
  --> verify_searchable(get_text关键词) --> 验证报告
```

1. **渲染**：`src/render.py: render_page(pdf,pno,dpi=200)->bytes`。新PDF以PNG为全页背景，原尺寸建页。
2. **OCR**：`src/llm_client.py + src/ocr_page.py`。`batch_plan.py`驱动批量，`out/pages/page_{pno:04d}.md + out/usage.jsonl`断点续跑，串行，失败重试2次。`llm_client` 支持 `LLM_OCR_BASE_URL/MODEL/KEY` 与 `--base-url/--model/--api-key` 双轨，三协议 `chat|responses|messages` 可切，`detail high|low|auto` 可调，`--extra-json` 任意字段直通（见3.3~3.4）。
3. **标号门禁**：`prompts/ocr_system.md + prompts/notation_spec.md`约束，`src/check_notation.py`拦截。
4. **定位**：boxes可靠时`render_mode=3`按盒写入；否则fallback“全文隐藏副本+按行均匀分布”，保证可搜可复制。
5. **合并**：`src/postprocess.py: clean_md + merge_pages`，页间`<!-- PAGE n -->`，头部索引。`polish_with_llm` 同样三协议活透传。
6. **验证**：`src/verify_searchable.py: verify + compare_md`，查顺同化/逆同化/语音学命中页与可复制字数。

目录：

```
llm-ocr/
  prompts/ocr_system.md  notation_spec.md
  src/llm_client.py          # 三协议活透传核心：generic_request + chat/responses/messages
      ocr_page.py            # 单页OCR：--endpoint/--detail/--extra-json
      render.py  batch_plan.py  # 批量：同款活参，usage.jsonl 追加 endpoint/extra
      make_searchable.py  check_notation.py  postprocess.py  verify_searchable.py
  out/pages/  usage.jsonl  book.md  book_searchable.pdf
  tests/page21_150.png  cand_165/166/167.png  notation_cases.json
  .env.example  task_plan.md  README.md
```

---

## 3. LLM调用规范（灵活化后，覆盖旧死参写法 2026-09-03）

> 溯源：`bestruirui/octopus internal/server/handlers/relay.go:14` -> `router.NewGroupRouter("/v1").AddRoute("/chat/completions", Forward(APIFormatOpenAIChatCompletion)).AddRoute("/responses", Forward(APIFormatOpenAIResponse)).AddRoute("/messages", Forward(APIFormatAnthropicMessage))` + `looplj/axonhub llm/model.go + transformer/openai/{model.go, responses/model.go} + transformer/anthropic/model.go`
>
> **网关真相**：`internal/relay/handler.go:Forward()` 只读 `model`+`stream` 做选组与路由，**业务参数完全不校验直接 `raw.Body` 透传**；同协议 `sendPassthrough` 原样转发，跨协议走 `axonhub/llm/transformer` 自动转换。结论：**只要是官方合法 JSON，Octopus 都会原样发给上游**（仅禁止覆盖 `model`/`stream`，其余 `ParamOverride` 都能透传）。

### 3.1 三入站（Octopus 全开）

| 入站 | 路径 | 源码模型 | 用途 |
|---|---|---|---|
| Chat Completions | `POST /v1/chat/completions` | `axonhub llm/transformer/openai/model.go:Request` | OpenAI 兼容，最常用，OCR 默认 |
| Responses | `POST /v1/responses` | `axonhub llm/transformer/openai/responses/model.go:Request` | OpenAI Responses，推理省 token（实测 7.3s vs 19.1s） |
| Messages | `POST /v1/messages` | `axonhub llm/transformer/anthropic/model.go:MessageRequest` | Anthropic 兼容，第3条路由一直可用 |

`GET/POST /v1/files` 在 OCT 上 `404` 未实现，无需也无法走 `file_id`；生图是另一条 `tools:[{type:image_generation}]` 别混。

### 3.2 完整参数面（均可透传，不再写死）

**Chat** 必选 `model`, `messages`；关键可选：`temperature/top_p/seed/frequency_penalty/presence_penalty/logit_bias/logprobs/top_logprobs`, `max_tokens`(deprecated)/`max_completion_tokens`, `reasoning_effort(none/minimal/low/medium/high/xhigh/max)/reasoning_budget/reasoning_summary`, `tools/tool_choice/parallel_tool_calls`, `response_format{type, json_schema}/verbosity/stop/stream+stream_options{include_usage}`, `store/service_tier/metadata/modalities`, `thinking{type:enabled/disabled}`, `extra_body`。视觉：`messages[].content: [{type:text},{type:image_url, image_url:{url: data:...|https://..., detail: high|low|auto}}]`。

**Responses** 必选 `model`, `input`；关键可选：`instructions`(→system), `input: string | [{type:message/function_call/reasoning, role, content:[{type:input_text/input_image, image_url, detail}]}]`（**必须包在 message 内**，顶层平铺会 400），`max_output_tokens`, `reasoning{effort, summary, max_tokens}`, `text{format{type:text/json_object/json_schema, schema, strict}, verbosity}`, `tools[{type:function/image_generation/web_search/custom/namespace}]`, `truncation/include/background/previous_response_id/stream`。

**Messages** 必选 `model`, `messages`, `max_tokens`；关键可选：`system: string|[{type:text}]`, `temperature(0-1)/top_k/top_p`, `thinking{type:enabled/disabled/adaptive, budget_tokens, display}`, `output_config{effort:low/medium/high/max}`, `tools/tool_choice`, `stop_sequences/cache_control`。视觉：`content: [{type:text},{type:image, source:{type:base64|url, media_type:image/png|jpeg|gif|webp, data|url}}]`。

跨协议自动转：`channel.go:buildOutbound()` 优先级 `want > Anthropic > Responses > Chat`，所以你在网关侧调三种格式都会被转成上游真实协议。

### 3.3 代码层：活透传（`src/llm_client.py`）

旧写法写死两行是瓶颈：`{"max_tokens":100000,"temperature":0}`。现已改为全透传：

```python
# 最灵活：你拼任意合法 JSON，我只注入 model/base_url/key 后 POST
from llm_client import generic_request
payload, usage = generic_request(
    {"messages": [{"role":"user","content":"hi"}], "temperature": 0.2, "reasoning_effort": "low"},
    endpoint="chat",  # "chat"|"responses"|"messages" 或 "/v1/chat/completions" 等
    base_url="http://YOUR_GATEWAY_HOST:2113/v1",
    model="OC/muse-spark-1.3-contributor-free",
)

# 型别入口（均 **kwargs 直通 body）
from llm_client import chat_completions, responses_create, anthropic_messages
text, usage, raw = chat_completions(messages, temperature=0.2, max_completion_tokens=8192, reasoning_effort="low")
text, usage, raw = responses_create("hi", instructions="你是OCR...", max_output_tokens=8192, reasoning={"effort":"low"})
text, usage, raw = anthropic_messages(messages, max_tokens=4096, system="你是OCR...", temperature=0.3)

# 视觉便捷（detail + **kwargs）
from llm_client import chat_vision, responses_vision, anthropic_vision
text, usage = chat_vision(png_bytes, prompt, detail="high", temperature=0, max_tokens=10000)
text, usage = responses_vision(png_bytes, prompt, detail="high", reasoning_effort="low")
text, usage = anthropic_vision(png_bytes, prompt, system="你是OCR...", temperature=0)

# 远端 URL 免 base64（网关拉取）
from llm_client import chat_vision_url, responses_vision_url, anthropic_vision_url
text, usage = chat_vision_url("https://example.com/a.png", prompt, detail="high")

# 兼容旧调用（仍可用）
from llm_client import chat_text, chat_vision_auto
text, usage = chat_text("hi", temperature=0.2)
```

关键实现：`_resolve_endpoint(base_url, api_path)` 自动防 `/v1/v1` 双写，支持 `bare host:port` 自动补 `http://` 与 `https` 443 判定；`_merge_body_kwargs` 合并 `extra`/`extra_json`；默认 `max_tokens=100000 / temperature=0` 仅在调用方未指定时生效，指定即覆盖。

### 3.4 CLI 活参（`ocr_page / batch_plan / postprocess`）

```bash
# 单页 OCR：三协议 + 任意参数透传
python src/ocr_page.py --image tests/cand_165.png --output out/cand_165.md
python src/ocr_page.py --image tests/cand_165.png --output out/a.md \
  --endpoint chat --detail high \
  --extra-json '{"temperature":0.2,"max_completion_tokens":8192,"reasoning_effort":"low"}'

python src/ocr_page.py --image tests/cand_165.png --output out/resp.md \
  --endpoint responses --extra-json '{"reasoning":{"effort":"low"},"max_output_tokens":8192}'

python src/ocr_page.py --image tests/cand_165.png --output out/msg.md \
  --endpoint messages --system "你是中文 OCR 引擎" --extra-json '{"temperature":0.3}'

# 远端 URL 免 base64
python src/ocr_page.py --image-url https://example.com/page.png --output out/url.md --detail high

# PDF 渲染后 OCR
python src/ocr_page.py --pdf "E:/down/.../语音学教程 增订版.pdf" --page 0 --dpi 200 --output out/page0.md --endpoint chat

# 批量（断点续跑，usage.jsonl 追加 endpoint/extra）
python src/batch_plan.py --dry-run --endpoint responses --extra-json '{"temperature":0.2,"reasoning_effort":"low"}'
python src/batch_plan.py --pdf "E:/down/.../语音学教程 增订版.pdf" --output-dir out --endpoint chat --detail high --extra-json '{"max_tokens":4096}'

# 后处理 polish 也活透传
python src/postprocess.py --input out/page21.md --output out/page21.clean.md --polish \
  --endpoint chat --extra-json '{"temperature":0,"max_tokens":8192}'
```

环境变量 / CLI 优先级（`--extra-json` 合并进 body，优先级最高）：

| 层级 | 变量/参数 | 示例 | 优先级 |
|---|---|---|---|
| 环境变量 | `LLM_OCR_BASE_URL` | `http://YOUR_GATEWAY_HOST:2113/v1` | 高 |
| 环境变量 | `LLM_OCR_MODEL` | `OC/muse-spark-1.3-contributor-free` | 高 |
| 环境变量 | `LLM_OCR_KEY`（兼容 `LLM_OCR_API_KEY`/`OCTOPUS_API_KEY`） | `sk-octopus-...` | 高 |
| CLI | `--base-url/--model/--api-key/--endpoint` | 见各 `main()` | 最高 |
| CLI | `--extra-json` | `'{"temperature":0.2,"reasoning_effort":"low"}'` | 最高（合并进 body） |
| CLI | `--detail` | `high\|low\|auto` | 高 |
| 代码默认 | `DEFAULT_BASE_URL/MODEL` + `max_tokens=100000/temperature=0` | — | 最低（可被覆盖） |

敏感信息只读 env/`.env`，不在日志回显。`host:2113` 为 host 网络，不走 Nginx，公网与 `127.0.0.1:2113` 同口；也支持切官方 `https://api.openai.com/v1`。

### 3.5 官方 SDK 直连也可用（最灵活的替代）

Octopus 完全兼容官方 SDK，绕过本项目 `llm_client` 直接调也行：

```python
import openai
client = openai.OpenAI(base_url="http://YOUR_GATEWAY_HOST:2113/v1", api_key="sk-octopus-...")
resp = client.chat.completions.create(
    model="OC/muse-spark-1.3-contributor-free",
    messages=[{"role":"user","content":[
        {"type":"text","text": open("prompts/ocr_system.md", encoding="utf-8").read()},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,...","detail":"high"}}
    ]}],
    temperature=0.2, max_completion_tokens=8192,
)
```

### 3.6 已修正的旧错误

- 模型名固定 `OC/muse-spark-1.3-contributor-free`，裸名 400。
- `chat` 默认 `max_tokens:100000`，`responses` 默认 `max_output_tokens:100000`，推理页 `<16` 会 400，现可被 `--extra-json` 覆盖为更小值（如 4096）。
- `responses` 必须包 `input:[{role, content:[input_text, input_image]}]`。
- 视觉 `data:` 为本地先 base64（`--image` 自动转），`https:` 为远端由网关拉取，均走 `llm.MessageContentPart` 透传；IPA 推荐 `detail:"high"`。

---

## 4. 标号位置语法（最高优先级）

| 位置 | 写法 | 例 |
|---|---|---|
| 正下 | `$\underset{x}{Y}$` | `$[\underset{w}{t}]$` `$[\underset{w}{k}]$` `$[\underset{w}{tɕ'}]$` |
| 正上 | `$\overset{x}{Y}$` | `$\overset{h}{t}$` |
| 左上 | `${}^{x}Y$` | — |
| 左下 | `${}_{x}Y$` | — |
| 右上 | `$Y^{x}$` | `$[k^{h}]$` `$[a^{35}]$` |
| 右下 | `$Y_{x}$` | `$[t_{w}]$`类按实际位置用 |

规则：公式外套`$...$`，花括号配对，禁止`[t_w]/[tw]/^w`，`tɕ/tʂ`复合音标与`ʰ ː ˈ ³⁵`原样保留。`ocr_system.md`含3正例2反例，`check_notation.py + notation_cases.json(≥12条)`做门禁。

---

## 5. 可能遇到的问题与对策

1. **代理污染**：SDK读代理失败。对策：只用`http.client/curl`直连或本项目 `llm_client`（已走直连），`LLM_OCR_BASE_URL` 可切官方。
2. **空回/超时**：大图+ xhigh 推理慢。对策：`--extra-json '{"reasoning_effort":"low"}'` 缩短首字，`timeout=300`，串行，重试2次。
3. **传参错误**：错名/缺 `max`/`input` 未包 message 致 400。对策：固定 `OC/` 前缀 + `responses` 必包 message + `detail high|low|auto`；活参用 `--extra-json` 透传。
4. **写死参数不灵活**：旧代码锁 `max_tokens/temperature`。对策：已改为 `**kwargs` 全透传，任意官方字段都能过。
5. **boxes不准**：差字漂移。对策：抽查标题/首段/IPA段3行，误差>20px或文本不一致即fallback，不直写。
6. **图片超限**：300dpi b64约2.4MB超 1.5MB。对策：默认200dpi，>1.5MB自动降150dpi重试。
7. **旧层污染**：错字映射。对策：永远新建PDF（背景PNG+新层），不复用原页对象。
8. **中文路径写失败**：`EISDIR`。对策：Python `pathlib.write_text(encoding="utf-8")` 落盘。
9. **成本**：全书约200万tokens。对策：先跑164/165/166三页IPA端到端（已验证双链路 200），确认后再全量；小 `max_tokens` 可省费用。

---

## 6. 开箱向导与默认真相表（v5 封装终版）

### 6.1 向导（不改代码跑全流程）

```bash
# 1. 填 URL/Key（--key-stdin布尔开关；管道/TTY/优先级见§6.4）
python -m src.cli config --init   # base_url → Key掩码/--key-stdin → models选择/手填 → endpoint responses → concurrency 4 → 写.env
# 2. 拉取并选择模型（任何GET失败均降级手填不阻塞）
python -m src.cli models          # 唯一入口 GET /v1/models
# 3. 脱敏核验
python -m src.cli config --check  # key仅 sk-**** + key_len
# 4. 设并发 → 选endpoint → dry-run看303页规划 → 1页真跑
python -m src.cli batch --pdf "<书>.pdf" --output-dir out --dry-run --endpoint responses
python -m src.cli probe --image tests/cand_165.png --endpoint auto   # 落 out/probe.json
python -m src.cli batch --pdf "<书>.pdf" --output-dir out --start 0 --end 1 --concurrency 4 --endpoint responses
# 5. 薄问答（解环，interactive.py为shim）
python -m src.cli --tui
```

### 6.2 默认真相表

| 项 | 库默认 | CLI默认 | 备注 |
|---|---|---|---|
| endpoint | `chat` | `responses` | `--endpoint chat` 回旧行为 |
| concurrency | `1` | `4` | `1..20` 硬校验；`>20` 拒绝；`>8` 警告 |
| max_tokens | `8192`（chat `max_tokens` / responses `max_output_tokens`） | 同左 | opt-in 双键：chat `'{"max_tokens":100000}'` / responses `'{"max_output_tokens":100000}'`；anthropic `4096 experimental` 不动 |
| timeout | POST `90`（`--timeout` 覆盖） | 同左 | POST-only；GET `/v1/models` 恒 15s single-try |
| detail | `high` | 同左 | IPA 推荐 high |

### 6.3 auto 1+1（形式语义）

`auto_vision()` 单点：先 `responses` single-try；仅当 `HttpError.status in (429, 500..599)` / `timeout` / `OSError` 时回退 `chat` single-try（1+1，不做 3× 后回退）；`400/401/403/404` 直接抛（fail-fast，不回退不重试）；回退独立于 `retries`（auto 下 legs 内部亦不重试，非 auto 才 3×）；`usage.jsonl` 记实际命中 `endpoint_normalized` + `endpoint_requested="auto"` + `attempt` 区分 leg。`messages` 为 experimental（不保证，不进 auto 集；CLI/库命中即 warn）。

### 6.4 并发与自适应

建议 `4`；`>8` 警告；自适应 `BoundedSemaphore`（初值=concurrency，复用同一 slot 含 auto 回退 leg）：遇 HTTP 429（每次计 1）→ 该 worker 退避 + 上限半减（下限 1）+ `429_count++`；回升 = 连续 20 成功 +1 至初值；耗时以实测 `elapsed_p50_ms/p95_ms` 为准（`--concurrency 20` 上限校验通过但耗时不承诺）。`LLM_OCR_PORT` 废弃（读到即 warn 忽略，`base_url` 唯一口径）。Key 不落 git、不在日志明文（仅 `sk-**** + key_len`；`--api-key` 即 warn）。

### 6.5 成本双轨

主表 probe 实测 ×303（responses ~330k / chat ~633k tokens）；2M 仅 worst-case 列（`dpi_benchmark` 外推，待校准）。

### 6.6 `.env` 模板

见 `.env.example`：`LLM_OCR_CONCURRENCY=4`、`LLM_OCR_DETAIL=high`、`LLM_OCR_ENDPOINT=responses`、`LLM_OCR_TIMEOUT=90`（POST-only 注释）+ models 示例；`.gitignore` 含 `.env` + `out/`。

## 7. 验证与交付（旧§6后移）

- `check_notation`全量0 issues，cases全过。
- 双层PDF背景`maxdiff=0`，`get_text`命中顺/逆同化，可复制字数>0。
- `usage.jsonl`可断点续跑，追加 `endpoint/extra` 便于复盘，最终交付`book.md + book_searchable.pdf + 验证报告`（`out/probe_ocr_chat.json` chat 200 19.1s + `out/probe_ocr_responses.json` responses 200 7.3s 双证据）。
- `python -m py_compile src/*.py` 全过；`batch_plan --dry-run --endpoint {chat|responses|messages}` 均验证 303 页规划正常。
