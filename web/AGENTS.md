# AGENTS.md — 桌面应用 UI 项目规则

> 供 AI 编码助手（Claude Code / Cursor / Codex / Copilot）自动读取。
> 放在项目根目录。子目录可放各自的 AGENTS.md，**就近规则优先**。
> 最后更新：2026-09-12

---

## Context

跨平台桌面应用：Tauri 2.x 外壳 + Web 技术栈渲染 UI。

目标：做出"看起来像原生商业软件"的界面，不是"网页套壳"。

验收心态：把窗口截图发出去，没人第一反应说"这是个 Electron 应用"。

---

## 1. 技术栈（不要偏离，不要自行升级换代）

| 层 | 选型 |
|---|---|
| 外壳 | **Tauri 2.x**（不是 v1） |
| 前端 | React 19 + TypeScript（strict） |
| 构建 | Vite 6+ |
| 样式 | Tailwind CSS v4（用 `@tailwindcss/vite` 插件，无 tailwind.config.ts） |
| 组件 | shadcn/ui |
| 图标 | lucide-react |
| 图表 | Recharts（走 shadcn chart 封装） |
| 动画 | CSS transition 优先；复杂序列用 `motion` |
| 状态 | Zustand（跨组件）/ TanStack Query（异步数据） |
| 路由 | TanStack Router 或 React Router |
| IPC 类型 | tauri-specta（见 §4） |

**不要引入第二套组件库。** 特别是 Ant Design / MUI / Chakra / Element Plus —— 它们的默认视觉是"网页后台管理系统"，会把桌面应用做成一坨 SaaS 后台。

---

## 2. 项目结构

```
project-root/
├── src/                          # 前端
│   ├── components/ui/            # shadcn/ui 组件（CLI 生成，可改）
│   ├── components/               # 业务组件
│   ├── features/                 # 按功能域组织
│   ├── stores/                   # Zustand
│   ├── hooks/
│   ├── lib/
│   ├── styles/tokens.css         # 设计 token（见 §5）
│   ├── bindings.ts               # tauri-specta 生成，勿手改
│   └── main.tsx
├── src-tauri/
│   ├── capabilities/*.json       # 权限（见 §3.3）
│   ├── src/
│   │   ├── lib.rs                # 入口：插件注册 + 命令注册
│   │   ├── main.rs
│   │   ├── commands/             # #[tauri::command]
│   │   └── managers/             # 业务逻辑
│   ├── Cargo.toml
│   └── tauri.conf.json
└── AGENTS.md
```

---

## 3. Tauri v2 铁律

### 3.1 v1 写法一律视为错误

AI 训练数据里大量是 Tauri v1，**这是本项目最高频的翻车点**。以下写法出现即为 bug：

| 错误（v1） | 正确（v2） |
|---|---|
| `@tauri-apps/api/tauri` | `@tauri-apps/api/core` |
| `@tauri-apps/api/window` | `@tauri-apps/api/webviewWindow` |
| `@tauri-apps/api/fs` | `@tauri-apps/plugin-fs` |
| `@tauri-apps/api/dialog` | `@tauri-apps/plugin-dialog` |
| `@tauri-apps/api/http` | `@tauri-apps/plugin-http` |
| `@tauri-apps/api/process` | `@tauri-apps/plugin-process` |
| `@tauri-apps/api/os` | `@tauri-apps/plugin-os` |
| `@tauri-apps/api/cli` | `@tauri-apps/plugin-cli` |
| `Window` / `WindowBuilder` | `WebviewWindow` / `WebviewWindowBuilder` |
| `WindowUrl` | `WebviewUrl` |
| `Manager::get_window` | `Manager::get_webview_window` |
| `app.get_window("main")` | `app.get_webview_window("main")` |
| `parent_window` | `parent_raw` |
| `tauri.allowlist` | `capabilities/*.json` 权限系统 |
| 配置根键 `tauri` | 根键 `app`，`bundle` 提到顶层 |
| `tauri.bundle` | 顶层 `bundle` |
| `tauri::api::process` | `tauri_plugin_shell::ShellExt` |
| `appWindow` 全局单例 | `getCurrentWindow()` |

**不要凭记忆写 Tauri API。** 不确定就先读 `https://v2.tauri.app/llms.txt`。

### 3.2 官方插件对照表（要用就用这个，不要自己造）

安装：`pnpm tauri add <name>`（自动改 Cargo.toml、lib.rs、package.json、capabilities）

| 需求 | 插件 | JS 包 |
|---|---|---|
| 窗口尺寸/位置持久化 | `tauri-plugin-window-state` | `@tauri-apps/plugin-window-state` |
| 应用设置持久化 | `tauri-plugin-store` | `@tauri-apps/plugin-store` |
| 单实例锁 | `tauri-plugin-single-instance` | 无 JS API |
| 自动更新 | `tauri-plugin-updater` | `@tauri-apps/plugin-updater` |
| 打开外部文件/URL | `tauri-plugin-opener` | `@tauri-apps/plugin-opener` |
| 系统信息 | `tauri-plugin-os` | `@tauri-apps/plugin-os` |
| 文件系统 | `tauri-plugin-fs` | `@tauri-apps/plugin-fs` |
| 原生对话框 | `tauri-plugin-dialog` | `@tauri-apps/plugin-dialog` |
| 系统通知 | `tauri-plugin-notification` | `@tauri-apps/plugin-notification` |
| 日志 | `tauri-plugin-log` | `@tauri-apps/plugin-log` |
| 全局快捷键 | `tauri-plugin-global-shortcut` | `@tauri-apps/plugin-global-shortcut` |
| 重启/退出 | `tauri-plugin-process` | `@tauri-apps/plugin-process` |
| 窗口定位（托盘弹窗） | `tauri-plugin-positioner` | `@tauri-apps/plugin-positioner` |
| HTTP（绕开 CORS） | `tauri-plugin-http` | `@tauri-apps/plugin-http` |
| 文件访问 scope 持久化 | `tauri-plugin-persisted-scope` | — |
| 崩溃/异常上报 | `sentry-tauri`（社区） | — |

### 3.3 权限（capabilities）

默认**全部拒绝**，必须显式开。

- 权限文件放 `src-tauri/capabilities/`，每个文件一个 `identifier`
- 按窗口分配：`"windows": ["main"]`，不要全局开
- 只开用到的那一个命令，不要 `fs:default` 一把梭
- 文件系统必须限定 scope（如 `$APPDATA/myapp/**`），禁止整个盘

窗口相关常用权限（自绘标题栏必加）：

```
core:window:allow-start-dragging
core:window:allow-minimize
core:window:allow-toggle-maximize
core:window:allow-close
```

### 3.4 安全

- `tauri.conf.json` 里必须设置 CSP，不要留 `null`：
  `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' asset: data: blob:`
- 所有 `#[tauri::command]` 的入参必须**反序列化到强类型 struct 并校验**（长度、范围、路径白名单）。前端是不可信输入源。
- 不要在命令里拼接字符串执行 shell。
- 高安全场景可启用 Isolation Pattern（拦截并校验所有 IPC 消息）。
- 上线前 `pnpm tauri build` 后检查 devtools 在生产不可用。

---

## 4. IPC 类型安全（必须）

不要手写 `invoke("getUserProfile", {...})` 这种字符串调用 —— 命令名拼错、字段名拼错都不会报错，只在运行时炸。

用 **tauri-specta** 生成类型安全绑定：

```rust
#[tauri::command]
#[specta::specta]
pub fn get_user_profile(id: String) -> Result<UserProfile, String> { ... }

// lib.rs
let builder = tauri_specta::Builder::<tauri::Wry>::new()
    .commands(tauri_specta::collect_commands![get_user_profile]);

#[cfg(debug_assertions)]
builder.export(specta::ts::ExportConfig::default(), "../src/bindings.ts").unwrap();

tauri::Builder::default().invoke_handler(builder.invoke_handler())
```

前端：

```ts
import { commands } from "@/bindings";
const res = await commands.getUserProfile(userId); // 全类型推断
```

Rust 是唯一真相源，改签名后前端立刻报错。

---

## 5. 设计 Token

**所有颜色、间距、圆角、动效时长必须走 token。禁止硬编码 hex、禁止魔法数字、禁止 `style={{}}` 里写颜色。**

完整可用文件见 `tokens.css`。核心约束：

```
颜色    ：OKLCH，语义命名（background / foreground / muted / border / ring / primary / destructive）
          只允许一个强调色。层级靠字重 + 灰度，不靠颜色数量。
字号    ：12 / 13 / 14 / 16 / 20 / 24 / 32（桌面端正文 13-14px，不是 16px）
字重    ：400 / 500 / 700（中文不要用 600，字体回退会变成 400 或 700）
间距    ：4 的倍数
圆角    ：控件 6-8px，卡片 10-12px，最大 12px
边框    ：1px，低对比度（比背景深一档）
阴影    ：最多两级，禁止彩色阴影
动效    ：120ms / 180ms / 240ms，ease-out，必须支持 prefers-reduced-motion
```

暗色模式：`.dark` 类切换 + 跟随系统 `prefers-color-scheme` + 手动开关。

---

## 6. 中文排版规范（CJK 专用，西文规则不适用）

中文字身是方块、无基线、无空格分词，**直接套西文排版参数会很难看**。

```css
/* 字体栈：拉丁在前，中文回退 */
font-family: Inter, -apple-system, "PingFang SC", "Microsoft YaHei",
             "Source Han Sans SC", "Noto Sans CJK SC", sans-serif;

/* 行高：中文必须比西文大 */
西文正文 1.5  →  中文正文 1.7
中文标题 1.3

/* 其他 */
text-align: justify;              /* 中文正文两端对齐 */
text-justify: inter-ideograph;
line-break: strict;               /* 严格避头尾：标点不出现在行首/行尾 */
overflow-wrap: break-word;
letter-spacing: 0;                /* 中文不要额外字距 */
```

规则：

1. **行长**：桌面端每行 35-42 个汉字。超过就加 `max-width`，不要撑满窗口。
2. **正文颜色**：用深灰（如 `--foreground` 的 90%），不要纯黑 `#000`。
3. **加粗**：中文用 `font-weight: 700`。500/600 在中文字体里常无对应字重，会被静默替换。
4. **中英混排**：中英文/数字之间留约 1/6 em 空隙。能用 `text-autospace` 就用，否则在渲染层插入细空格。
5. **避免寡字**：段落最后一行只剩一个字要处理（调整容器宽度或 `text-wrap: pretty`）。
6. **按钮文字**：中文按钮比英文窄，最小宽度要单独给，不要照搬英文尺寸。
7. **不要给中文加 `text-transform: uppercase`**，没有意义。
8. 数字/表格列右对齐并用 `font-variant-numeric: tabular-nums`。

---

## 7. 无障碍（不是可选项）

桌面应用同样受无障碍约束，且键盘用户比例更高。

- **优先原生元素**。用 `<button>` 不要用 `<div onClick>`。ARIA 只在不存在的语义上补，不是给烂 HTML 打补丁。
- **focus 必须可见**：`focus-visible` 给 2-3px 高对比 outline，**禁止 `outline: none` 而不给替代**。
- **Tab 顺序 = 视觉顺序**。禁止正数 `tabindex`。flex 反向布局时注意焦点顺序会错。
- **模态/抽屉/弹出层**：打开时焦点移入并**锁在内部**循环，背景 `inert`，Esc 关闭，关闭后**焦点返回触发元素**。
- **复合组件**（菜单、Tab、树、表格）按 WAI-ARIA APG 实现方向键导航、Enter/Space 激活、Esc 取消。
- **滚动区可聚焦**：长列表容器加 `tabIndex={0}`，否则键盘用户滚不动。
- 状态变化（`aria-expanded` / `aria-selected` / `aria-busy`）要实时更新，加载/错误要播报。
- 尊重 `prefers-reduced-motion`。

---

## 8. 桌面原生感（与网页的分水岭，逐项必须实现）

| # | 要求 | 实现方式 |
|---|---|---|
| 1 | 原生菜单栏 | Tauri `Menu` / `Submenu` API，File / Edit / View / Window / Help；macOS 全局菜单栏，Windows/Linux 窗口内 |
| 2 | 系统托盘 | `TrayIconBuilder`，右键菜单含"显示/隐藏/退出"；macOS 用 `icon_as_template(true)` |
| 3 | 窗口状态持久化 | `tauri-plugin-window-state`，一行注册即可 |
| 4 | 单实例 | `tauri-plugin-single-instance`，第二次启动时唤回已有窗口 |
| 5 | 多窗口 | 偏好设置独立开窗，不要全塞主窗口 |
| 6 | 原生对话框 | `plugin-dialog` 的 open/save/message/ask，不要自己画 |
| 7 | 原生右键菜单 | Tauri `Menu::popup`，不要在 DOM 里自绘 |
| 8 | 关闭行为 | 主窗口关闭默认应"隐藏到托盘"而不是退出（`WindowEvent::CloseRequested` + `api.prevent_close()`） |
| 9 | 快捷键 | `Cmd/Ctrl+W` 关窗、`Cmd/Ctrl+,` 偏好、`Cmd/Ctrl+Q` 退出 |
| 10 | 系统主题跟随 | 读系统深浅色，同时提供手动覆盖并存到 store |
| 11 | 窗口尺寸约束 | 设 `minWidth` / `minHeight`，避免布局被压垮 |
| 12 | 首屏不闪白 | 窗口设 `"visible": false`，前端准备好后 `show()` |

### 自绘标题栏（如需要）

```json
// tauri.conf.json
"app": {
  "macOSPrivateApi": true,
  "windows": [{
    "decorations": false,
    "transparent": true,
    "visible": false
  }]
}
```

必须处理的坑：

- **拖拽区**：用 `data-tauri-drag-region` 属性（不要用已废弃的 CSS `-webkit-app-region`）
- **macOS**：`decorations: false` 会同时丢掉窗口圆角，需要自己用 `transparent` + `border-radius` 补；红绿灯位置用 `trafficLightPosition` 调，**红绿灯要保留原生**，别自己画三个圆
- **Windows**：`decorations: false` 会**丢失 Snap Layouts（贴靠布局）**，需要 `tauri-plugin-frame` 或自行处理 HWND 恢复
- **Windows 11 材质**：用 `window-vibrancy` 的 `apply_mica()`（Win11）/ `apply_acrylic()`（Win10 1809+）。**已知 bug：部分 Windows 11 构建下 Mica 首次显示不生效，需触发一次 resize 或 `center()` 才刷新**，要做 fallback
- **macOS 材质**：`apply_vibrancy(NSVisualEffectMaterial::Sidebar/HudWindow)`，需要 `transparent: true` + `macOSPrivateApi: true`
- `decorations: false` 在 macOS 和 Windows 行为**不一致**，必须在两端分别实测

### 关于"玻璃拟态"

不要在 Windows 上把 Acrylic 当装饰贴在卡片上 —— 性能差且不像原生。
macOS 的 vibrancy 是**系统材质**，用在侧边栏/工具栏上是正确的，不算滥用。
判断标准：**系统自带应用会不会这么做**。

---

## 9. 性能

- **长列表必须虚拟化**：>100 行用 TanStack Virtual / react-window，禁止全量渲染
- **重计算移出主线程**：Web Worker 或 Rust 侧
- **减少 IPC 次数**：批量传数据，不要循环 invoke
- **图片**：WebP/AVIF，列表内懒加载，给固定宽高防抖动
- **避免大面积 `backdrop-filter` 和动画阴影**，这是 WebView 渲染最慢的两件事
- 生产构建在 `Cargo.toml` 加：

```toml
[profile.release]
opt-level = "z"
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

- 启动目标 < 1s。首屏只加载必要资源，其余懒加载。

---

## 10. 禁止项（"AI 味 / 网页味"黑名单）

- ❌ 紫色/蓝色渐变背景
- ❌ emoji 当图标（用 lucide）
- ❌ 圆角 > 12px 的大胶囊按钮
- ❌ 到处都是彩色阴影、发光、外发光
- ❌ 长文本居中排版
- ❌ 无意义的模糊和玻璃效果（见 §8 判断标准）
- ❌ Lorem ipsum / 假数据占位
- ❌ 自绘右键菜单、自绘滚动条、自绘文件选择器
- ❌ Ant Design / MUI 那套后台管理视觉
- ❌ 手写 `invoke("字符串命令名")`
- ❌ 硬编码颜色和间距
- ❌ `outline: none`
- ❌ 一次性生成整个应用

---

## 11. 工作流（严格遵守）

### 11.1 先规划

1. 产出 `spec.md`：需求、数据模型、界面清单、每个界面的状态
2. 产出设计 token 确认稿（配色/字号/间距），**等我确认后再写代码**
3. 拆成小任务，一次只做一个：一个组件、一个页面、一个功能
4. 顺序：布局骨架 → 排版留白 → 配色组件 → 交互细节
5. 频繁提交，每步可回滚

### 11.2 每步都要"看见"结果

**不要盲写样式。** 每完成一个界面就验证渲染：

- **最快路径**：Tauri 前端就是 Vite dev server。加 `vite-plugin-tauri-in-the-browser`（dev-only），就能在普通浏览器打开 `http://localhost:1420` 看到同一份 UI，用 Chrome DevTools / Playwright / Chrome DevTools MCP 截图检查。
- **要连原生调用**：用 `tauri-remote-ui`，让浏览器接管真实 Tauri 窗口。
- **Tauri 专属**：`tauri-pilot`（Playwright 对 Tauri 无效，因为用 WebKitGTK 不是 Chromium）。

看到截图 → 找问题 → 改 → 再看。这一步是"好看"与"能看"的分界线。

### 11.3 每个界面必须齐的状态

| 状态 | 要求 |
|---|---|
| 正常 | — |
| 空 | 有插图/说明/下一步操作，不是空白页 |
| 加载 | Skeleton，不要转圈占满屏 |
| 错误 | 说明原因 + 重试按钮 |
| 无权限 | 说明 + 引导 |
| 超长内容 | 截断 / 换行 / tooltip |
| 窗口极窄 | 布局不崩 |

---

## 12. 验收清单

见 `desktop-ui-checklist.md`。交付前逐项过。

---

## 13. Commands

```bash
pnpm tauri dev                              # 开发（同时起 Vite + Rust）
pnpm tauri build                            # 生产构建
pnpm tauri add <plugin>                     # 加官方插件（自动配权限）
pnpm dlx shadcn@latest add <component>      # 加组件
pnpm dlx shadcn@latest docs <component>     # 拉组件文档进上下文
pnpm tsc --noEmit                           # 类型检查
pnpm lint                                   # ESLint
pnpm format                                 # Prettier
```

## 14. 不确定时先读文档

- Tauri：`https://v2.tauri.app/llms.txt`（或 `llms-small.txt` / `llms-full.txt`）
- 插件索引：`https://v2.tauri.app/plugin`
- v1→v2 迁移对照：`https://v2.tauri.app/start/migrate/from-tauri-1`
- shadcn/ui：`https://ui.shadcn.com/llms.txt`
- 完整资源清单见 `desktop-ui-ai-resources.md`
