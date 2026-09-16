# 双层可检索 PDF 制作全面技术评估与演进方案设计规范
*(Double-Layer Searchable PDF Architecture Assessment & Evolution Specification)*

**文档版本**：v1.0.0  
**制定日期**：2026-09-16  
**对应工程**：`llm-ocr` / `src/make_searchable.py` / `src/geom_align.py` / `src/geom_extract.py` / `src/verify_searchable.py`  
**归档状态**：技术架构蓝图与分阶段实施基线

---

## 目录
1. [执行摘要与设计原则](#1-执行摘要与设计原则)
2. [现有双层 PDF 制作链路深度评估（As-Is Assessment）](#2-现有双层-pdf-制作链路深度评估as-is-assessment)
   - 2.1 排版引擎与布局规划（Layout Planning）
   - 2.2 几何数据源与对齐机制（Embedded vs. External vs. Fallback）
   - 2.3 字体度量与字符映射（Font Metrics & Zero-Loss CJK/IPA/Symbols）
   - 2.4 表格几何重建与单元格映射现状
   - 2.5 质检验证与多维门禁系统（Verification Gates）
   - 2.6 已知瓶颈与质量边界（Known Limitations）
3. [大纲书签树与逻辑页码体系演进设计（Metadata & Navigation）](#3-大纲书签树与逻辑页码体系演进设计metadata--navigation)
   - 3.1 基于 Markdown 标题的层级大纲（TOC / Outlines）自动注入
   - 3.2 物理印刷印号自动识别与 PDF 逻辑页码（Page Labels）映射
4. [版面重建与排版引擎底层升级（Layout Engine Enhancements）](#4-版面重建与排版引擎底层升级layout-engine-enhancements)
   - 4.1 字体资产解耦与跨平台内置回退体系（Bundled Typeface System）
   - 4.2 词级别水平微调与墨迹投影对齐（Ink Histogram Alignment）
   - 4.3 复杂合并表格与无线表格几何解算器（Table Grid Solver）
   - 4.4 公式块（Formula Block）与特殊标记原子保护机制
5. [提示词与输出契约演进（Prompt-Engine Co-Design）](#5-提示词与输出契约演进prompt-engine-co-design)
   - 5.1 标题锚点与大纲元数据约定
   - 5.2 复杂表格 HTML 增强语义约定
   - 5.3 脚注与图注的语义化标签
6. [桌面端质检交互与透视预览设计（GUI & Inspection Preview）](#6-桌面端质检交互与透视预览设计gui--inspection-preview)
   - 6.1 文字层半透明叠加透视预览器（Overlay Diff Inspector）
   - 6.2 质检报告卡片与可疑页一键定位协议
7. [分阶段落地实施路线图（Implementation Roadmap）](#7-分阶段落地实施路线图implementation-roadmap)

---

## 1. 执行摘要与设计原则

双层 PDF（Searchable Raster-Backed PDF）通过在扫描光栅图像底层注入一层完全不可见的文字层，让用户既能看到原始高清扫描版面，又能执行精确划词、复制、全局搜索以及屏幕朗读。

在学术古籍、语言学著作（如含大量 IPA 国际音标、复杂发音图表、多栏对照、方言调查表）的生产场景下，双层 PDF 的技术要求极为严苛。本演进方案遵循以下四大基石原则：

1. **绝对零丢失（Zero-Loss Invariance）**：  
   OCR 识别出的 Markdown 文本，其包含的每一个有效字符（中文字符、ASCII、扩展拉丁字母、IPA 组合变音符、数学符号、标点）在底层文本层中必须 100% 存在，字符多重集（Multiset）严格一致，绝不因几何排版超宽、越界而被截断或丢弃。
2. **视觉与几何紧密吻合（Visual-Geometric Alignment）**：  
   不可见文字层字符的物理边界框（Bounding Box）、字号、基线高度必须与扫描光栅图上的笔画油墨完全重合；划词高亮框必须紧贴字形，严禁出现字号微缩（0.5pt 隐形堆叠）、字框漂移或垂直重叠。
3. **阅读顺序真实保真（Deterministic Reading Order）**：  
   从双层 PDF 框选并复制出的文本流，必须严格符合人类自然的阅读次序。双栏、插图标注、行间公式、表格行列等必须拓扑有序，杜绝跨栏穿插或图注碎片插入正文段落。
4. **无缝降级与确定性兜底（Graceful Fallback）**：  
   当扫描图像破损、版面极其复杂导致几何信息缺失或低相似度时，排版引擎必须自动安全降级至等距容量流（Fallback Capacity Flow），标明降级范围并记录质检报告，保证文件立即可用且可追溯。

---

## 2. 现有双层 PDF 制作链路深度评估（As-Is Assessment）

### 2.1 排版引擎与布局规划（Layout Planning）
当前核心实现位于 `src/make_searchable.py` 的 `plan_boxed_lines()` 与 `_insert_boxed_lines()`：
- **字号与水平拉伸解耦（Umi-OCR 拟合思想）**：  
  放弃了早期“因宽度不足而全局缩小子号”的策略，转为以行框高度确定真实字号（`box_height * 0.92`），宽度差异通过水平伸缩系数 `hscale = box_width / measured_width` 处理，显著提升了划词时的字形匹配度。
- **换行与段落拆分**：  
  首先按嵌入的 `\n` 进行逻辑行切分（解决 MinerU 块内多行堆积问题），随后对超宽行通过 `_wrap_line` 进行测宽贪婪折行，有效避免了 PyMuPDF 对超出页边字符的静默截断。
- **垂直基线对齐**：  
  实现了 `_center_baseline_offset()`，利用字体的实际 `ascender` 与 `descender` 度量值动态计算基线位置，纠正了过去写死 `y0 + 0.35 * fontsize` 造成的系统性垂直偏移。

### 2.2 几何数据源与对齐机制（Embedded vs. External vs. Fallback）
系统支持四种几何模式（`geo_source`: `auto` / `external` / `embedded` / `fallback_only`）：
1. **Embedded（源 PDF 内嵌文字层提取）**：  
   通过 `src/geom_extract.py` 消费 PyMuPDF 原生 `get_text("dict")`，支持 `/Rotate` 与 `cropbox` 仿射变换，自动计算字符乱码率 `garble_ratio`。当乱码率低于 0.3 且行数充足时启用，通过 `geom_align.py` 的行级 `SequenceMatcher` 与 Markdown 进行文本匹配（门限 `SIMILARITY_THRESHOLD = 0.85`）。
2. **External（外部版面分析模型，如 MinerU content_list）**：  
   通过预处理生成的 `boxes.json`（0-1000 归一化坐标）直接注入块框。外部源具备强大的段落、标题、表格、公式分区能力，能达到近 100% 的对齐覆盖率。
3. **Fallback Capacity Flow（单层容量流兜底）**：  
   针对无几何信息的纯图片页或低相似度未匹配文本，废弃了早期“0.5pt 全文压缩条 + 8pt 伪行”的双副本模式（该模式会导致复制字符数翻倍破坏质检），改为单一均布容量槽，并在阅读序判定中标记 `order_exempt = True`。

### 2.3 字体度量与字符映射（Font Metrics & Zero-Loss）
当前建立了基于字符分类的多字体路由策略（`_font_segments`）：
- **CJK 汉字与全角标点**：路由至内置 `china-s` 字体（避免 Arial 将中文或中文字符映射为空字符 NUL）；
- **IPA 国际音标与扩展拉丁**：路由至 `IPA_FONT_PATH`（`C:\Windows\Fonts\segoeui.ttf`），覆盖 `æ ç ð ø œ ŋ þ ß ə ɛ ɔ ɑ β ʔ ʂ tɕ` 等音标；
- **数学与排版符号**：路由至 `SEGOE_SYM_FONT_PATH`（`C:\Windows\Fonts\seguisym.ttf`），覆盖 `∅ ✘ ‡ ² ¬ ₀` 等符号；
- **基础 ASCII**：路由至 `helv`。

### 2.4 表格几何重建与单元格映射现状
- **外部 MinerU 路径**：支持从 `table_raw_html` 或 `table_cells` 提取单元格，结合 `table_grid` 矩阵将整块表格拆分为按单元格定位的独立微文本框（`_table_cell_lines`），支持 `table_cell_gate` 质检（实测 Recall 达到 0.9928）。
- **内嵌/纯 Markdown 路径**：当缺乏精确单元格坐标时，GFM 表格仅能通过空格连接各列并在整行中铺设，遇到列宽悬殊或复杂跨行时，鼠标划词会出现跨列漂移。

### 2.5 质检验证与多维门禁系统（Verification Gates）
`src/verify_searchable.py` 实现了行业顶级的严格质检套件：
- **Zero-Loss 字符多重集全等门**：`_char_multiset(expected) == _char_multiset(actual)`，对每页所有非空白字符进行逐字频度比对；
- **实际阅读顺序门（Actual Reading Order Gate）**：直接从输出 PDF 提取 `words` 坐标，结合 `ROW_TOL = 12.0`、`COL_TOL = 20.0` 进行视线拓扑排序，与排版计划交叉校验；
- **几何偏差门（Geometric Distance Gate）**：`GEO_TOL = 16.0`（约 10 pt），检验渲染文字实际中心与物理源框的距离。

### 2.6 已知瓶颈与质量边界（Known Limitations）
1. **Windows 字体强依赖**：代码写死 `C:\Windows\Fonts\`，在 Linux/macOS 容器或无该字体的 Windows 精简版上会回退失效，破坏音标零丢失；
2. **大纲（Bookmarks）与逻辑页码（Page Labels）缺失**：当前生成的双层 PDF 不包含目录书签树，PDF 阅读器中无法快速跳转章节；PDF 物理第 150 页无法映射为原书印刷页码“· 123 ·”；
3. **公式断行碎片化**：长 LaTeX 公式若被当成普通文本进行字宽切分，可能被拆成多个碎片行，破坏公式整体的物理拓扑；
4. **长行词级字距抖动**：两端对齐的印刷文本，仅靠单行等比水平拉伸 `hscale`，在行中间的文字与实际笔画油墨仍存在微小像素级偏差；
5. **前端质检黑盒**：桌面端缺乏对对齐质量的直观预览手段，用户只能依赖文本日志，无法直观查验文字层是否对齐。

---

## 3. 大纲书签树与逻辑页码体系演进设计（Metadata & Navigation）

### 3.1 基于 Markdown 标题的层级大纲（TOC / Outlines）自动注入
#### 目标
利用 LLM OCR 转录出的标准 Markdown 标题层级（`#`、`##`、`###`、`####`），在生成双层 PDF 时直接向 PDF 文档对象（`fitz.Document`）注入完整的层级大纲树。

#### 算法与数据结构
1. **标题解析与坐标捕获**：
   在 `make_searchable.py` 的页级循环中，解析每一页的 Markdown：
   ```python
   # 匹配标题: # 一级标题, ## 二级标题, etc.
   HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
   ```
2. **目标锚点定位（Destination Calculation）**：
   - 提取标题文本内容后，在当前页生成的排版规划（`plan_boxed_lines`）中检索该标题第一行的首字坐标 `(x0, baseline)`；
   - 构造 PyMuPDF TOC 条目元组：`[lvl, title, page_number, {"to": fitz.Point(x0, baseline), "zoom": 0}]`。
3. **整册目录树生成与注入**：
   ```python
   toc: list[list[Any]] = []
   for pno in range(doc.page_count):
       page_headings = _extract_page_headings(md_dict, pno, page_plans[pno])
       for lvl, title, dest_pt in page_headings:
           toc.append([lvl, title, pno + 1, {"to": dest_pt, "zoom": 0}])
   doc.set_toc(toc)
   ```
4. **验收标准**：
   在 `verify_searchable.py` 中增加大纲检验：`doc.get_toc()` 返回的条目数应与 Markdown 中标题总数完全一致，各标题跳转页码准确无误。

---

### 3.2 物理印刷印号自动识别与 PDF 逻辑页码（Page Labels）映射
#### 目标
传统扫描 PDF 翻阅困难的核心原因是：PDF 物理第 1 包含封面、版权页、序言，导致正文第 1 页可能对应 PDF 物理第 15 页。通过自动提取正文页脚的物理印号，绑定为 PDF 标准的 Page Labels，使阅读器输入书上印刷的页码即可精准直达。

#### 规则与实现逻辑
1. **印号识别规则**：
   从页面底部的文本行或 Markdown 尾部提取印号：
   - 罗马数字前言：`i`, `ii`, `iii`, `iv`, `I`, `II`, `III`
   - 正文阿拉伯数字：`· 12 ·`, `- 12 -`, `12`, `第 12 页`
2. **区间分段与单调性校验（Label Ranges Construction）**：
   扫描各页检测出的印号，构建单调递增区间。对于未识别出印号的扉页、插页，自动根据前后页插值推算：
   ```python
   # 构造 PyMuPDF set_page_labels 规则列表
   # 每个字典包含: startpage (0-based), style, prefix, firstpagenum
   labels = [
       {"startpage": 0, "style": "r", "prefix": ""},       # 序言: i, ii, iii...
       {"startpage": 14, "style": "D", "prefix": "", "firstpagenum": 1} # 正文: 1, 2, 3...
   ]
   doc.set_page_labels(labels)
   ```

---

## 4. 版面重建与排版引擎底层升级（Layout Engine Enhancements）

### 4.1 字体资产解耦与跨平台内置回退体系（Bundled Typeface System）
#### 架构改造方案
1. **开源字体资产打包引入**：
   在项目 assets 目录（如 `src/assets/fonts/`）内嵌两款高保真精简开源字体：
   - `CharisSIL-Compact.ttf`（语言学协会官方权威 IPA / 全套扩展拉丁 / 变音组合符字体，完全开源）；
   - `STIXTwoText-Symbols.ttf`（高精度数学、箭头与排版符号字体）。
2. **多级回退链设计（Fallback Font Resolver）**：
   ```python
   def resolve_ipa_font() -> Path:
       # 1. 优先使用随包分发的内置高保真字体 (跨平台绝对保证一致)
       bundled = Path(__file__).parent / "assets" / "fonts" / "CharisSIL.ttf"
       if bundled.is_file():
           return bundled
       # 2. 其次使用系统 Segoe UI
       if IPA_FONT_PATH.is_file():
           return IPA_FONT_PATH
       # 3. 最终回退并记录 Warning
       return None
   ```
3. **收益**：
   无论在 Windows、Linux 服务器（Docker）还是 macOS 桌面，均能 100% 渲染所有 IPA 附加符号，彻底规避因缺少系统字体导致提取出空白或方块的问题。

---

### 4.2 词级别水平微调与墨迹投影对齐（Ink Histogram Alignment）
#### 问题诊断
当前排版规划中，整行文字按固定字宽线性累加 `x` 坐标；然而在古籍或铅字印刷体中，由于字符间距不均、标点挤压或两端对齐，行中段的字常与图像中的油墨有 2~5 像素偏差。

#### 解决方案：轻量级墨迹谷底对齐（Ink Valley Snapping）
1. **局部二值化与垂直投影（Vertical Projection Profile）**：
   在整行物理边界框 `(x0, y0, x1, y1)` 内截取原图灰度切片，计算列灰度投影积分曲线；
2. **字符间隙波谷检测**：
   当文本包含多个拉丁单词或汉字字块时，在理论字符间距窗口内搜索投影最低点（即字间白缝）；
3. **坐标轻量微调**：
   将理论累加坐标 `x_calc` 微调至最近的白缝波谷 `x_snap`，限定最大调整量 `<= 0.2 * fontsize`，确保既贴合真实油墨，又不破坏单调连续性。

---

### 4.3 复杂合并表格与无线表格几何解算器（Table Grid Solver）
针对含有合并单元格（`rowspan`/`colspan`）或无明显物理线格的复杂方言对照表：
1. **表格结构拓扑网格化**：
   结合 OCR 模型输出的结构化表格，建立物理网格坐标系 `Grid[R, C]`；
2. **水平/垂直投影辅助定界**：
   通过分析表格区域内的横向白条与纵向白条确定列宽向量与行高向量；
3. **单元格独立隔离插入（Cell-Isolated Placement）**：
   每个单元格的内容强制限制在其对应的 `CellRect` 内独立自适应缩放与插入，绝不跨越相邻单元格的物理分界线，杜绝鼠标在表格中横向划词时意外选中上下行相邻列文字。

---

### 4.4 公式块（Formula Block）与特殊标记原子保护机制
1. **原子块标识契约**：
   在 Markdown 中以 `$$...$$`、`$...$` 包裹的独立公式，或带 `is_equation=True` 元数据的区域，标记为 `AtomicBlock`。
2. **保护策略**：
   - 禁用任何按字符强行切分或折行；
   - 采用整体等比自适应缩放：
     ```python
     if is_equation:
         scale = min(1.0, box_width / measured_width, box_height / measured_height)
         target_fontsize = max(MIN_FONT_PT, fontsize * scale)
         # 作为单一行或保持原有结构插入，严禁切断
     ```
   - 保持阅读序拓扑合并，避免公式内的分子分母在阅读序校验中被判定为穿插断裂。

---

## 5. 提示词与输出契约演进（Prompt-Engine Co-Design）

双层 PDF 排版引擎的上限高度依赖上游 OCR 模型输出的纯净度与结构化信息。针对双层 PDF 排版反哺要求，对 `prompts/ocr_system.md` 进行定向扩充。

### 5.1 标题锚点与大纲契约
* **新增要求**：  
  严禁将正文第一行误标为 `# 一级标题`，居中书名、章节名按 `# 标题` 准确还原，章节序号与标题文字之间保留标准空格，便于排版引擎准确提取 PDF TOC 目录层级。

### 5.2 复杂表格 HTML 增强语义
* **扩充规则**：  
  当遇到多行表头、跨行跨列（`rowspan`/`colspan`）的复杂表格，或垂直旋转表格时，允许并推荐模型使用规范的 HTML `<table>...</table>` 格式输出。排版引擎原生解析 `<tr>`, `<td>`, `rowspan`, `colspan` 属性，与底层网格解算器直接对接。

### 5.3 脚注与图注的语义化标签约定
* **规范定义**：  
  - 页面下方的脚注一律使用 `[^1]: 注释正文` 格式输出；
  - 插图周围的说明文字统一使用 `<figure-caption>图 1-1 说明</figure-caption>` 标记；
  - **排版引擎对应行为**：排版引擎自动识别此类语义块，在阅读顺序推导中赋予其特定的层级优先级，彻底解决图注碎片插进正文段落导致的阅读序校验失败。

---

## 6. 桌面端质检交互与透视预览设计（GUI & Inspection Preview）

### 6.1 文字层半透明叠加透视预览器（Overlay Diff Inspector）
在桌面端 Tauri 客户端的 `SearchableView` 中引入专业级质检透视能力：

```
+-----------------------------------------------------------------------+
|  [单页检查]  [批量制作]  [双层检索构建]                                    |
+-----------------------------------------------------------------------+
|  源 PDF: sample.pdf  [选择]   模式: [ Auto (内嵌+模型混合) v ]           |
|  [√] 启用书签生成   [√] 绑定逻辑页码   [ 开始构建可检索 PDF ]               |
+-----------------------------------------------------------------------+
|  质检透视预览 (Page 21/472)   [ < 上一页 ] [ 下一页 > ]                  |
|  透视开关: [x] 显示文字框 (绿色)  [x] 显示识别字形 (半透明洋红)             |
|  +-----------------------------------------------------------------+  |
|  |  +-----------------------------------------------------------+  |  |
|  |  | 绪论                                                      |  |  |
|  |  | [绿色框: (120,45,280,75)] -> "绪论" (China-s 24pt)         |  |  |
|  |  | 现代汉语方言音系调查中，[tɕ] 与 [tʂ] 的对立至关重要...       |  |  |
|  |  | [半透明洋红文字完美覆盖在原书扫描图像笔画上方]            |  |  |
|  |  +-----------------------------------------------------------+  |  |
|  +-----------------------------------------------------------------+  |
|  指标面板:                                                            |
|  * 字符零丢失 (Zero-Loss): 100% (452/452)                            |
|  * 几何对齐覆盖率 (Aligned Coverage): 98.4%                           |
|  * 阅读顺序 (Reading Order): PASS                                     |
|  * 目录书签定位: 命中 "# 第一章 绪论" -> 物理页 21                     |
+-----------------------------------------------------------------------+
```

#### 技术实现协议
1. **后端渲染接口扩展（`serve.py`）**：
   - 新增 `GET /api/pdf/overlay_preview?path=&pno=&show_boxes=true&show_text=true`；
   - 利用 PyMuPDF 的绘图上下文（`page.draw_rect`）将不可见文本层的每个 `span` 真实边界框用半透明彩色矩形（如绿色为对齐行，黄色为 fallback 行）高亮勾勒，并把不可见文字用半透明红墨水直接打印在光栅图上，以 PNG 流形式高速吐给前端。
2. **前端无缝比对切换**：
   - 交互提供“纯原图 / 纯文本层 / 叠加透视”单选或快捷键切换，校对人员可以一眼看出任何字号微缩、字框偏离或标点悬空。

---

## 7. 分阶段落地实施路线图（Implementation Roadmap）

| 阶段 | 目标代号 | 核心任务与交付物 | 验收与回归标准 |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **P0 体验与导航闭环** | 1. 落地 Markdown 标题层级自动注入 PDF TOC 书签树；<br>2. 落地页脚印刷印号自动识别并绑定 PDF Page Labels；<br>3. 扩展现有 `verify_alignment` 输出大纲与页码检验项。 | `doc.get_toc()` 与 `doc.get_page_labels()` 100% 验证通过；现有 65 项 pytest 保持全绿。 |
| **Phase 2** | **P1 排版引擎健壮化** | 1. 内置跨平台 IPA 与符号开源字体（Charis SIL 等），移除对 Windows 系统盘的死锁依赖；<br>2. 公式块与原子符号标记的防拆行保护机制；<br>3. 复杂表格网格解析（Table Grid Solver）增强。 | Linux/无字体环境下 IPA 零丢失测试通过；公式块拆行导致的 reading-order 失败清零。 |
| **Phase 3** | **P2 深度对齐与透视 UI** | 1. 墨迹投影波谷微调对齐（Ink Histogram Snapping）；<br>2. 后端提供 `overlay_preview` 透视渲染切片流；<br>3. 桌面前端（React）集成透视质检面板与可疑对齐页直达。 | 划词高亮与扫描墨迹像素级贴合；前端直观展示绿色对齐框与洋红文字层。 |

---
*文档编制完成，符合 Goal 验收准则。*
