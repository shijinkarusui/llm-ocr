# llm-ocr 新前端 spec.md（按 desktop-ui-kit 重写）

> 状态：已完成（S1–S10，2026-09-13）。旧 Tkinter UI 已删除，打包双产物已验证。
> 旧 Tkinter UI（`app_gui.py` + `gui/` + `llm-ocr-gui.spec`）已在 S10 删除（新 UI 端到端通过后）。

---

## 0. 背景与目标

- 项目：llm-ocr 仓库根，Vision-LLM OCR 工具链（扫描版中文教材 → Markdown + 双层可搜索 PDF）。
- 引擎 `src/`（Python，37 tests 全绿）**不动**，只重写前端。
- 新前端：Tauri 2.x 外壳 + React 19，视觉按 `out/uikit/` 四文件执行（AGENTS.md / tokens.css / checklist / ai-resources）。
- 已用《现代汉语八百词》（764 页）验证引擎链路：离线渲染、双层构建、`responses` 真实 OCR 均通过。新 UI 验收同样用八百词 p0 做端到端。

## 1. 技术栈（pin，不偏离）

| 层 | 选型 |
|---|---|
| 外壳 | Tauri 2.x（本机装 rustup 后编译；MSVC BuildTools 18 已在，无需重装 VS） |
| 前端 | React 19 + TypeScript strict + Vite 6 + Tailwind v4（`@tailwindcss/vite`，无 config 文件） |
| 组件/图标 | shadcn/ui + lucide-react（**唯一**组件库） |
| 状态/异步 | Zustand（连接参数/Key/任务状态，常驻内存） + TanStack Query（models 列表、轮询进度） |
| 桥接 | `serve.py`（stdlib only，`http.server` + 引擎 import）跑 `127.0.0.1:21139`，前端 `fetch` 经强类型 `lib/engine.ts` 调用 |
| 打包 | PyInstaller `serve.exe`（sidecar）+ Tauri NSIS；prompts 随包 |

偏离说明（已评估）：引擎调用走 HTTP 而非 `invoke`，故 tauri-specta 不适用 Rust 侧（Rust 只注册官方插件命令，天然类型安全）；引擎侧类型安全由 `lib/engine.ts` + 契约测试保证。路由：5 视图用侧边栏状态切换，不引入 Router（桌面应用单窗，无深链需求）。

## 2. 目录结构

```
llm-ocr/
  src/ prompts/ tests/ requirements.txt   # 不动
  serve.py                                 # 新增：桥接服务（本 spec §3）
  serve.spec                               # 新增：PyInstaller 打包 serve.exe
  spec-new-ui.md                           # 本文件
  web/                                     # 新增：Tauri 工程
    src/{components/ui,components,features,stores,hooks,lib,styles/tokens.css,main.tsx}
    src-tauri/{capabilities/*.json,src/{lib.rs,main.rs},Cargo.toml,tauri.conf.json}
    AGENTS.md                              # 由 uikit 的 desktop-ui-AGENTS.md 落盘
  # 删除（最后一步）：app_gui.py  gui/  llm-ocr-gui.spec
```

## 3. 桥接层 serve.py API 契约

- 只绑 `127.0.0.1:21139`，单实例锁文件；CORS 仅放行 Tauri 与 localhost Vite。
- Key 只在请求 body 里出现，常驻前端内存；serve.py 只打 `sk-**** len=N`，永不落盘、不进日志明文。
- 长任务（批量/双层）为 job 制：POST 启动返回 `job_id`，`GET /api/jobs/{id}` 轮询，`POST /api/jobs/{id}/cancel` 取消。

| 方法 | 路径 | 对应旧 UI | 说明 |
|---|---|---|---|
| GET | /api/health | — | `{ok, version, engine:"src"}` |
| GET | /api/models?base_url= | 连接·拉取模型 | 调 `llm_client.list_models`，key 走 header `X-LLM-Key` |
| POST | /api/probe | 连接·单图探活 | `{base_url,model,key,endpoint,detail,timeout}` → `{endpoint_used,text,usage,elapsed_ms}`（用 tests/cand_165.png 渲染字节） |
| POST | /api/ocr/image | 单页·图片 | `{png_b64,...}` → `{markdown}` |
| POST | /api/ocr/pdf-page | 单页·PDF单页 | `{pdf_path,pno,dpi,...}` → `{markdown, png_b64_preview}`（附 150dpi 预览） |
| POST | /api/ocr/url | 单页·图片链接 | `{image_url,...}` → `{markdown}` |
| POST | /api/batch/dry-run | 批量·规划页数 | `{pdf_path,start,end}` → `{total_pages, planned_pages, first5[]}` |
| POST | /api/batch/run | 批量·开始批量 | 同上 + 引擎参数 → `{job_id}`（调 `run_batch`，usage.jsonl 断点续跑保持） |
| GET | /api/jobs/{id} | 批量·刷新进度 | `{status,pages_ok,tokens,skipped,failed,p50_ms,p95_ms,summary?}` |
| POST | /api/searchable/build | 双层·构建并验证 | `{pdf_path,out_dir,geo_source,keywords}` → job；完成返回 `{pdf,bytes,pages,copyable_chars,hit_pages}` |
| POST | /api/notation/check | 参数·标号校验 | `{dir}` → `{files,total,codes,worst[]}` |
| GET | /api/prompt | 参数·看提示词 | 返回 ocr_system.md 原文 |
| GET | /api/config/check | 连接·脱敏核验 | 脱敏回显（无 key 明文） |

## 4. 界面清单（5 视图 + 日志 + 状态栏）

外壳：左侧源列表式侧边栏（连接/单页/批量/双层/参数）+ 主内容区 + 底部日志控制台（可折叠）+ 24px 状态栏（网关·模型·链路·任务状态）。

| 视图 | 核心控件 | 状态矩阵 |
|---|---|---|
| 连接 | 网关地址/Key（密码框）/模型下拉（拉取+手填）/同步·拉取·探活·核验按钮/结果区 | 空（未连接说明）/加载（Skeleton）/错误（原因+重试） |
| 单页 | 来源三选（图片/PDF单页/图片链接）/文件选择（**原生对话框** plugin-dialog）/页码/渲染预览图/识别按钮/Markdown 结果+保存 | 空/加载/错误+重试/超长结果滚动区 |
| 批量 | PDF+输出目录/起止页/Dry-Run/开始/进度条+成功·tokens·skipped·failed·p50/p95/刷新 | 空/运行中（可取消）/完成/失败+续跑提示 |
| 双层 | 原PDF/输出目录/geo_source 四选/关键词/构建并验证/结果（路径·大小·页数·可复制字数·命中页数） | 同上 |
| 参数 | 链路三选/清晰度三选/并发·超时·DPI·重试滑杆/透传JSON编辑/应用/看提示词/标号校验 | 非法JSON即时报错；并发>8 警告条 |

旧 UI 行为保留：Key 只驻内存；批量断点续跑读 usage.jsonl；探活用 cand_165；默认参数（endpoint responses / high / 并发 10 / 超时 120 / DPI 200 / 重试 2 / extra 默认值照抄 State）。

## 5. 设计 token 确认稿（直接采用 tokens.css）

- `out/uikit/desktop-ui-tokens.css` 原样落 `web/src/styles/tokens.css`，单一强调色蓝（oklch 255），圆角基准 10px，字号 13-14 正文、行高 1.7，中文加粗 700。
- 暗色：`.dark` + 跟随系统 + 手动开关（tauri-plugin-store 持久化）。
- 待你确认：强调蓝是否保留（Kit 默认），还是换藏青 `#2F4B9B`（旧 UI 主色）？默认按 Kit 蓝。

## 6. Tauri 配置

- 插件：`window-state`（窗口记忆）/ `store`（设置）/ `single-instance` / `dialog`（原生文件框）/ `opener`（打开输出目录）/ `process`（退出）/ `notification`（批量完成通知）。
- capabilities 按窗口逐项开，无 `*:default`；fs scope 仅限用户所选目录（dialog 返回路径即授权）。
- CSP：`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' asset: data: blob: http://127.0.0.1:21139`（预览图走桥接）。
- 原生菜单（文件/编辑/视图/窗口/帮助）+ 托盘（显示/隐藏/退出）；关闭到托盘；`Ctrl+W` 关窗、`Ctrl+,` 参数页、`Ctrl+Q` 退出；minWidth 980 / minHeight 680；`visible:false` 首屏后 show。
- 自绘标题栏：**第一版不做**（用原生装饰，避免 Snap Layouts/Mica 坑），后续按需加。

## 7. 打包方案

1. `serve.spec` 打 `serve.exe`（console=False；datas 含 prompts/ + tests/cand_165.png；沿用 llm-ocr-gui.spec 的 excludes 瘦身经验，目标约 35MB）。
2. Tauri `externalBin` 注册 serve sidecar，随包 prompts；NSIS 安装包。
3. 打包前门：`pnpm tsc --noEmit` 零错、`vite build` 通过、引擎 `pytest` 37 绿、八百词 p0 端到端（探活→单页→双层→verify）通过。

## 8. 验收标准

- uikit `desktop-ui-checklist.md` A-E、G、I 全项可指认；F 桌面项按 §6 实现项验收（单实例/托盘/原生框/快捷键/窗口记忆/CSP）；H 性能：批量页虚拟化（>100 行页列表时）、无循环 IPC、冷启动 <1s（serve 常驻后）。
- 不做：玻璃拟态、渐变、自绘右键菜单、第二组件库、emoji 图标。

## 9. 实施步骤（频繁提交，每步可回滚）

1. S1 脚手架：`pnpm create tauri-app`（React+TS+Vite）+ Tailwind v4 + shadcn 初始化 + tokens.css + AGENTS.md 落盘；`vite build` 验证。
2. S2 桥接：`serve.py` + `lib/engine.ts` + 契约测试（health/models/dry-run 全离线可测）。
3. S3-S7 五视图（连接→单页→批量→双层→参数），每视图配四态，浏览器截图自检。
4. S8 Rust 工具链（rustup）+ `tauri dev` 联调 + capabilities/CSP/托盘/菜单。
5. S9 打包（serve.spec → externalBin → NSIS）+ 八百词 e2e。
6. S10 删旧 UI（app_gui.py/gui/llm-ocr-gui.spec）+ README 更新 + checklist 终检。

## 10. 风险与回退

- rustup 下载+编译耗时（首编约 10-20min）：S1-S7 先纯 Web 形态在浏览器跑，不阻塞。
- F 盘 exFAT 上 pnpm：已实测硬链/symlink/junction 均 OK；若装包失败回退 `node-linker=hoisted`。
- WebView2 152 已在；低版本机器由 NSIS bootstrapper 补。
- 任何一步失败：git 回滚到上一步 tag，引擎与旧 UI 不受影响（旧 UI 删除是最后一步）。
