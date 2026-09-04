# Plan Review Log: 双层PDF文字层坐标对齐修复
Started 2026/9/4 17:21:03. MAX_ROUNDS=5. Reviewer: DSH subagent (read-only).

## Round 1 — Infrastructure note (2026-09-04)

- 外部独立 reviewer 通道不可用：DSH `subagent` 探针失败（无 closing message）；`workflow` `agent()` 返回 null。按 codex-review 技能规则不静默重试，转为主代理对抗审阅（非独立 VERDICT，不作收敛判定）。
- 计划事实断言已只读独立核实：源 PDF 存在（F:/DSH工作区/book-to-quiz-pipeline/data/语音学教程.pdf）；303 页；抽样页 0/1/99 词框数 30/16/64、每页 1 张整页图、garble_ratio 0.0556/0.0667/0.0、页矩形约 426x646pt。Route A“内嵌层几何可用”前提成立，但内嵌层确有 OCR 误差（如 p1 取样词 “:W?32e”），影响 S3 对齐假设。

## Round 1 — Main-agent adversarial review (substitute for unavailable reviewer)

1. P0-1 水平居中系统性偏差 vs 验收 p50≤25：S4 只处理“超宽缩字号（只缩不放）”，插入仍以 (x0, y0+fontsize) 左对齐；插入字体与内嵌层原始字体度量不同，行窄于框时中心必然左偏，缩字号后更偏。Fix：按实测宽度水平居中 x = x0+(box_w-text_w)/2，且用插入文本实际 bbox 中心计算指标。
2. P0-2 垂直基线口径不一致：基线 y0+fontsize 使可见字形中心偏下约 0.1-0.2 行高，若指标按“插入框中心 vs 几何框中心”必系统性偏移。Fix：统一用 get_text("words") 的实际插入框中心；写入基线改 y0+ascent（或 y1-descent）并自证。
3. P0-3 md 与内嵌文本源冲突：内嵌层是另一 OCR 产物（抽样 p1 出现错乱词如 “:W?32e”），SequenceMatcher 行级对齐对任何内嵌误差敏感，强行对齐会错切/丢字。Fix：每行设相似度门限（如 ratio≥0.85），低于门限整行走容量流/fallback 并计入 fail 报告。
4. P0-4 表格/双栏“行级对齐”前提不成立：GFM 表格线性化（行拆格空格连接）与内嵌层的单元格/列几何不同构，cell 集合重排只是启发式；竖排/旋转表列序乱时无法保证“字不丢+位置对”。Fix：表格页走 cell 级匹配（cell 回指二分匹配，未匹配 cell 落容量流），验收加“单元格级召回率”。
5. P0-5 容量流“平均字宽”定义缺口：CJK 全宽、ASCII/IPA 半宽按平均宽切片会系统性高/低估容量（CJK 多则丢字、ASCII 多则溢出）。Fix：逐字符实测宽度贪婪切片（复用 _wrap_line 思路），平均字宽只作预算。
6. P0-6 覆盖率口径可被稀释：容量流“覆盖率恒 1”若计入 boxed 覆盖率，≥95% 门不区分对齐质量。Fix：分 boxed_aligned 与 covered 两个口径，验收以 boxed_aligned 为准。
7. P1-7 页眉页脚与 md 归一化不同步：S1 剔除 header_footer 不参与正文对齐，S2 未同步从 md 剔除，会错位/残留。Fix：md 归一化同规则去页眉页脚（或按自身几何框插入但标 unreliability），报告记录。
8. P1-8 旋转/竖排页覆盖不足：计划未说明 /Rotate=90/270 时 get_text("words") 坐标与插入坐标系一致性，正文竖排（非表格）未定义。Fix：普查统计旋转页；几何统一到输出坐标系，竖排正文单独排序+容量流规则并进 golden。
9. P1-9 行聚类阈值 0.5 行高为拍脑袋：上标/IPA/表格会错并错拆。Fix：普查期用三样本 golden 以 get_text("dict") 的 line 数为参照标定阈值，而非手拍。
10. P1-10 S2 扩展 _as_text 是隐式语义变更：_as_text 被 fallback/aux IPA 共用，去符号会改变现有输出、关键词命中与 char 计数。Fix：新增 _as_text_normalized 与旧 _as_text 并存，旧路径行为不变（或重打并更新基线）。
11. P1-11 验收门 copyable_chars 偏差≤5% 口径过粗：全书总量对比掩盖页级问题；fallback 底部 0.5pt 带/重复铺会放大总量。Fix：改页级 char 差 + 报告 top-N 页。
12. P1-12 verify_alignment 接口未定义：需 boxes.json、md、输出 PDF 三个输入与报告输出参数。Fix：明确 --boxes-json/--md-json/--out-pdf/--report 并纳入 CLI/GUI 冒烟。
13. P1-13 “只缩不放+下限4-5pt+折子行”堆积边界：窄高行框折子行多行压同一基线会重叠。Fix：折子行按 step=行高/子行数 垂直均布，场景进 golden。
14. P1-14 依赖与字体资产：需确认 requirements.txt 已含 PyMuPDF；IPA_FONT_PATH 硬编码 C:/Windows/Fonts/arial.ttf 为 Windows-only，缺失仅“降级不断”不够，应打 warn 计入报告。
15. P1-15 0 words 页策略未定死：容量流需要“真实行位”，0 words 页无行位只能 fallback。Fix：明确“0 words → fallback+warn”，不伪容量流。
16. P1-16 Observability 缺口：align_report 缺每行相似度分布与失败原因分类。Fix：加 similarity histogram + 失败原因计数（低相似度/无框/超宽）。
17. P1-17 破坏性全量重打缺先导批次：303 页重打为一次性大动作，系统性问题难隔离。Fix：30 页试点+抽查通过后再全量。

## Round 1 — Main agent response / human gate note

- 无独立 reviewer 的 VERDICT：APPROVED/REVISE 判定，故不自动收敛。以上 17 条中 P0-1..P0-6 建议在实施前纳入计划修订（若采纳本轮作为有效审阅）。
- 等待用户决定：采纳本会话审阅意见修订计划并进入人工签核 / 修复 subagent 通道后重跑独立审阅 / 终止。

## Round 2 — Main-agent adversarial review (continued, substitute for unavailable reviewer)

- 独立 reviewer 通道仍不可用（本会话无 DSH subagent/workflow 工具），继续以主代理只读对抗审阅；本 log 不构成独立 VERDICT。
- 已结合源码核对（make_searchable.py / verify_searchable.py / gui/tab_searchable.py / README）补充下列问题；Round 1 的 17 条未修订前仍全部有效。

### 新增/补充发现

1. **P0-A boxes.json 契约未钉死**：`_page_boxes()` 同时接受“含 `lines` 的单页容器”和“按页号索引的整册容器，页值可为 lines 列表或 `{reliable, lines}`”。计划只写“产出 boxes v2”，未规定顶层结构、页键、`reliable` 语义、box 字段（text/box）与坐标归一化。若不钉死，`make_searchable` 可能静默走 fallback，覆盖率和验收全部失真。Fix：在 S3 明确 schema 示例，并加单测直接喂 `_page_boxes()`。
2. **P0-B “文本不可用页按真实行位铺”自相矛盾**：没有可用几何时不存在“真实行位”；容量流只能利用 md 行断/段落伪行，或者只能 fallback。计划不能宣称“覆盖率恒 1 且退化为真实行位”。Fix：明确无几何页的行槽来源（md 换行 or 等距），并把“覆盖率”拆成 aligned/covered 两个口径。
3. **P0-C fallback 双副本破坏字符数验收**：现有 `_insert_fallback()` 同时写 0.5pt 全文块 + 8pt 行分布，抽出的 copyable_chars 是 md 的约 2 倍。计划若保留 fallback，验收“copyable_chars 与 md 偏差≤5%”会在任何 fallback 页直接失败。Fix：fallback 改单副本，或验收按页去重/排除 fallback 页并单列报告。
4. **P1-D garble_ratio 阈值未接线且未标定**：`garble_ratio()` 目前只在 `verify_searchable.py`，`make_searchable` 不会自动调用；0.3 是拍脑袋阈值。Fix：把模式判定抽成共享函数，并用三样本 golden 标定阈值，报告分布。
5. **P1-E 自研词级聚类是多余复杂度**：PyMuPDF `get_text("dict")` 已提供 block/line/span 及真实行 bbox；用 words 按 0.5 行高重聚容易拆错上标/IPA/表格。Fix：优先消费原生 line 对象，只对双栏/竖排做排序后再对齐；若确需自聚类，给出与原生 line 的误差对比。
6. **P1-F “溢出率=0”可能数学上不可达**：若某个框内出现不可断的长 token（长英文、IPA 组合、单元格），在 4-5pt 下限下仍可能宽于框；再折行会拆词。Fix：定义“可断 token”边界；不可断超宽项走 warn/fallback 或允许该页溢出率>0，不要拿“绝对 0”当硬门。
7. **P1-G geo_source 优先级未定义**：`make_searchable()` 现签名已有 `boxes_dict_or_None`。新增 `geo_source=embedded` 时，外部 boxes 是否忽略？校验冲突？GUI/CLI 如何暴露？Fix：明确优先级（embedded > external > fallback 或显式互斥）并加参数校验。
8. **P1-H 验收“框选复制与所见一致”无法由中心偏差证明**：`verify_alignment` 只能测几何/IoU/字符量，测不了 PDF 阅读器里框选复制出来的顺序与内容是否一致。Fix：在抽查页加“提取文本逐框比对 + 阅读序重排”自动化检查；人工框选仅作最终抽验。
9. **P1-I `_usable_boxes` 会拒绝合法单行页**：现有可用性检查要求 `len(lines)>=2` 且纵向跨度 ≥50/1000；标题页/单行表格可能被误判 fallback。计划提到“单行页”但没说明是否放宽。Fix：单行页有几何就用单行框，无几何才 fallback。
10. **P1-J 验收 5% 字符偏差仍偏粗**：按全书总量掩盖页级缺失/多余。Fix：改为页级字符差 + top-N 报告；“md 一字不丢”与“偏差≤5%”表述要统一，建议“非空白字符逐页 0 丢失，标点/排版差异单独计”。

### Round 2 — Main agent response

- 以上 P0-A/B/C 与 Round 1 P0-1..P0-6 建议在实施前并入计划；P1-D..J 作为实现细节约束写入。
- 仍无独立 REVIEWER VERDICT；不自动进入 codex-build。等待用户决定：是否按这些意见修订计划并人工签核。

## Subagent Round 1 — Independent reviewer (subagent_fork)
状态：成功。Reviewer: DSH subagent_fork（只读）。

### Reviewer critique（原文要点）
1. **boxes.json schema 未与 `_page_boxes()` 对齐**：现有消费端支持多种形态并强制 0-1000、`x1>x0`、`y1>y0`；计划未定义精确结构。Fix：S3给出JSON示例并加单测喂 `_page_boxes()`。
2. **`geo_source` 调用链不明**：现有签名只接收外部 boxes；`embedded` 是函数内部实时抽取还是上层先产 boxes？Fix：明确“内嵌模式=内部自动走 geom_extract→align→写入”，外部 boxes 显式覆盖。
3. **“无几何页按真实行位铺”是伪命题**：没有 geometry 就没有“真实行位”。Fix：改为“无几何页走等距/md 伪行槽，并单列 covered-not-aligned”。
4. **`_usable_boxes()` 会拒绝合法单行页**。Fix：放宽为单行可靠 box 也接受。
5. **`_as_text()` 语义共享导致回归**。Fix：新增 `_as_text_normalized()`，保留旧函数。
6. **只有行级对齐无法满足表格 cell 级目标**。Fix：表格页增加 cell-level 写入路径。
7. **md 与内嵌文本不一致无门限**。Fix：每行相似度<0.85 时整行走容量流/fallback。
8. **未匹配 md 字符无落点**。Fix：定义未匹配字符→就近行尾/容量流/专属盒，按页零丢失。
9. **水平对齐方式被忽略**。Fix：按实测宽度保留原行对齐，居中时 `x=x0+(box_w-text_w)/2`。
10. **垂直基线未用字体真实度量**。Fix：用插入后实际 bbox 或 font.ascender 校准。
11. **“溢出率=0”不可达**。Fix：允许不可断 token 超宽计 warn。
12. **字体宽度与插入字体不一致**，IPA_FONT_PATH 硬编码 Windows 且可缺失。Fix：报告实际字体清单。
13. **覆盖率口径被稀释**。Fix：分 aligned_coverage 与 covered_coverage。
14. **“一字不丢”与“≤5%偏差”冲突**。Fix：页级非空白字符差异，fallback 单列。
15. **fallback 双副本破坏可复制字数**。Fix：fallback 单副本或按去重/页级排除。
16. **“框选复制与所见一致”无法自动验证**。Fix：增加输出 PDF 提取文本逐框/行排序后与 md 比对。
17. **`verify_alignment` 接口未定义**。Fix：明确 `--out-pdf/--md-json/--boxes-json/--report` 及字段。
18. **页码 0/1-based 映射未定死**。Fix：强制统一 0-based 并加映射单测。
19. **页眉页脚排除不同步**。Fix：S1/S2 同步剔除并记录。
20. **旋转/裁剪页未处理**。Fix：抽查 /Rotate，统一变换或非0旋转页 fallback 并报告。
21. **自研词级聚类是多余复杂度**。Fix：优先消费原生 dict line。
22. **CLI 入口位置未定义**。Fix：指定统一入口或明确独立脚本并更新 README。
23. **PyInstaller 打包未覆盖新模块**。Fix：spec 补 hiddenimports 或静态导入。
24. **输出替换无先导/备份**。Fix：30页试点、临时/版本号输出、原子替换。
25. **失败原因与观察性不足**。Fix：报告增加 per-line failure reason、similarity histogram、top-N 失败页。

### Reviewer final line
VERDICT: REVISE

### Main agent response
- 主代理裁量：25条全部采纳（其中部分与之前主代理审阅重合，均并入本版计划）。
- 已修订 `plan_text_layer_alignment.md`：
  - S1 改用原生 line、/Rotate 处理、放宽单行页；
  - S2 新增归一化函数、0-based 映射、页眉页脚同步；
  - S3 钉死 boxes v2 schema、拆分 aligned/covered、行相似度门限、cell 级匹配、未匹配字符落点、无几何伪行；
  - S4 水平居中/真实垂直基线、折子行均布、overflow_expected、geo_source 调用链、fallback 单副本、字体清单；
  - S5 verify_alignment 接口与报告、提取排序比对、页级零丢失；
  - S6 CLI 挂载、spec hiddenimports、30页试点+原子替换。
- 启动 Subagent Round 2 复查。

## Subagent Round 2 — Independent reviewer (subagent_fork)
状态：成功。Reviewer: DSH subagent_fork（只读）。检查 R1 是否被解决并找新问题。

### Reviewer critique（原文要点）
总体：Round 1 方向性意见大多已进入修订版，但仍有“只有目标、缺少可执行定义”的缺陷；当前仍不足以直接实现。
1. **中英/IPA 混排行没有字体切片规则**：现有 `_font_for_line()` 整行单选字体，混排会丢字/测量错。Fix：按 span/字符段切分，逐段测宽，加 golden。
2. **表格 cell 级路径缺少 cell 几何来源**：`get_text("dict")` 不保证给出 cell 边界。Fix：定义 cell box 来源（表格线/span聚合/显式输入），不可靠时整表容量流。
3. **aligned/covered 指标无可计算定义**：没有分子/分母和页级 aligned 条件。Fix：给出字符级比例与页面阈值。
4. **“提取文本逐框/逐行排序比对”缺排序算法**：双栏/竖排/表格/折子行阅读序未定义。Fix：定义确定性排序键并与 `_as_text_normalized()` 比较。
5. **未匹配 md 字符“三者之一”仍不确定**：无优先级/判定/阅读序约束。Fix：固定优先级“就近行尾→容量流→专属盒”。
6. **fallback 单副本具体形态和零丢失范围不清**。Fix：明确保留 8pt 行分布单层，fallback 页同样零丢失门。
7. **`geo_source` 与外部 boxes 逐页组合语义仍不完整**。Fix：给出签名并定义 external→embedded→fallback。
8. **/Rotate 与 cropbox 变换只有方向**。Fix：用 rotation_matrix/derotation_matrix/cropbox 并加 golden。
9. **CLI/报告职责重复**。Fix：make_searchable 产 align_report，verify_alignment 产 verify_report。
10. **“强制统一 0-based”与“兼容 0/1 双键”表述冲突**；出现未定义 `profile`。Fix：内部 0-based、边界兼容层归一，删除 profile。
11. **页眉页脚剔除没有检测规则**。Fix：定义位置+正则+跨页重复检测，单列剔除字符数。

### Reviewer final line
VERDICT: REVISE

### Main agent response
- 主代理裁量：11条全部采纳，已并入 `plan_text_layer_alignment.md`：
  - S1 旋转变换公式与 golden；
  - S2 页眉页脚检测规则、0-based 内部策略、移除 profile；
  - S3 cell box 来源、aligned/covered 数学定义、未匹配字符固定优先级；
  - S4 混排字体按 span 切分、fallback 单层且零丢失门、geo_source 签名与逐页组合；
  - S5 提取排序键、verify_report 与 align_report 分离；
  - S6 报告职责拆分。
- 启动 Subagent Round 3 复查。

## Subagent Round 3 — Independent reviewer (subagent_fork)
状态：成功。Reviewer: DSH subagent_fork（只读）。检查 R1+R2 是否解决并找新问题。

### Reviewer critique（原文要点）
总体：前两轮大部分已解决，但仍存在“目标有、可执行定义缺”的规格缺口。
1. **`geo_source` 内部矛盾**：三模式 + “external→embedded→fallback”逐页链导致非确定。Fix：四模式矩阵 `auto/external/embedded/fallback_only`。
2. **`aligned_coverage` 分子无可测字符关联规则**。Fix：定义逐字符成功标准（过阈值 + 插入 bbox 中心偏差≤N）并输出分类。
3. **页眉页脚“剔除”与零丢失冲突**。Fix：写入专属框并纳入零丢失，或正式排除并报告。
4. **“专属未匹配盒”未定义**。Fix：定义为合成页底容量槽或删除，证明容量流可保证零丢失。
5. **“容量流” vs “fallback”术语重叠**。Fix：统一为一个实现名/报告类别。
6. **混排字体切分可能破坏提取序列**。Fix：定义 canonicalization 并加混排提取单测。
7. **`verify_alignment` embedded 模式输入源不明**。Fix：必须消费 align_out/boxes.json，缺失 fail-fast。
8. **“hiddenimports 或静态导入”不是决定**。Fix：选 hiddenimports + frozen 冒烟。
9. **Garble 阈值校准无验收标准**。Fix：定义 golden 页集、预期分布、阈值和路径 guard。

### Reviewer final line
VERDICT: REVISE

### Main agent response
- 主代理裁量：9条全部采纳，已并入 `plan_text_layer_alignment.md`：
  - S1 garble 校准验收；
  - S2 页眉页脚纳入零丢失/显式 excluded；
  - S3 逐字符 aligned 判定、专属未匹配盒定义、术语统一为 fallback 容量流；
  - S4 geo_source 四模式矩阵、提取 canonicalization；
  - S5 verify 输入源钉死 align_out/boxes.json；
  - S6 确定 hiddenimports + frozen 冒烟。
- 启动 Subagent Round 4 复查。

## Subagent Round 4 — Independent reviewer (subagent_fork)
状态：成功。Reviewer: DSH subagent_fork（只读）。检查 R1-R3 是否解决并找新问题。

### Reviewer critique（原文要点）
总体：多数前轮意见已进入，但仍有关键规格缺口（#1-#4 P0）足以阻止直接实施。
1. **P0 boxes v2 缺 S5 排序所需元数据**：无 `line_id/column/cell_row/cell_col/subline_index/order`，verify 无法执行规定阅读序。Fix：加入 boxes v2 或 layout.json。
2. **P0 aligned>95% 聚合判定含糊**：未写清“≥95%的页达标”还是“每页达标”。Fix：明确“至少95%页满足页内≥95%；未达标列原因且零丢失”。
3. **P0 未匹配字符页底合成槽破坏阅读序**。Fix：放入最近真实行/格，或标记 `order_exempt` 排除出阅读序门。
4. **P0 混排字体只有切分没有布局公式**。Fix：给出逐段累计 x、居中/右对齐跨段计算、行 bbox 并集。
5. **P1 最小字号下可断行也可能无法溢出=0**。Fix：单字符宽>框宽时标 overflow_expected/fallback。
6. **P1 关键阈值仍是“如/范围”**。Fix：写死常量并入 align_report。
7. **P1 garble 门没全量验证，也不能代表对齐可行性**。Fix：Step 1 全量 per-line 混淆表；不足则逐行失败驱动。
8. **P1 header_footer_excluded 与零丢失冲突**。Fix：定义为“已由 fallback 覆盖且计入零丢失”，仅排除对齐归属。
9. **P1 auto 未定义 external 校验链**。Fix：external 仍过可用性/相似度，空/坏逐页回退并记录。
10. **P1 普通页“列优先再行优先”方向错误**。Fix：普通页 `(y,x)`，双栏才 `(column_order,y,x)`。
11. **P1 表格路径没有独立验收门**。Fix：表10-3加 cell 级 recall/precision 门并输出 table_unreliable。

### Reviewer final line
VERDICT: REVISE

### Main agent response
- 主代理裁量：11条全部采纳，已并入 `plan_text_layer_alignment.md`：
  - boxes v2 增加阅读序元数据；
  - 验收明确“≥95%页达标”聚合断言；
  - 未匹配字符优先最近真实几何，合成槽 `order_exempt`；
  - 混排布局公式与 bbox 并集；
  - 常量写死并报告；
  - Step 1 全量逐行混淆表；
  - header_footer_excluded 语义澄清；
  - geo_source auto external 校验链；
  - 排序键修正为 (y,x) 等；
  - 表格 cell 级验收门。
- 启动 Subagent Round 5 复查（MAX_ROUNDS=5）。

## Subagent Round 5 — Independent reviewer (subagent_fork) / DEADLOCK
状态：成功，但返回 REVISE。本轮为 MAX_ROUNDS=5 的最后一轮，未能获得 APPROVED。

### Reviewer critique（原文要点）
总体：前四轮大多已落实，但仍有 3 个 P0 阻塞，不可直接实施。
- **P0-1 阅读序校验是“循环验证”**：S5 让 verify 优先消费 boxes v2 的 order 元数据来排序，但 boxes 是写入前生成的预期顺序，不能证明输出 PDF 实际框选/复制顺序；写入位置错了也可能通过。Fix：从输出 PDF 实际 `get_text("dict")/words` bbox 重新推导排序，boxes 元数据只作交叉校验；增加原始提取顺序 vs boxes 期望顺序一致性检查。
- **P0-2 boxes v2 缺“行类别”字段**：报告类别有 aligned/fallback_capacity/header_footer_excluded，但 schema 的 source 只有 embedded/md/cells，writer/verify 无法按类别路由与统计，fallback 合成行可能被 `_page_boxes()` 当成可靠几何行。Fix：每 line 增加 `mode/category` 或分区存放，并加路由兼容单测。
- **P0-3 fallback/无几何页的 order_exempt 覆盖范围不明确**：只定义了页底合成槽豁免，文本不可用页的 md 伪行/等距槽未定义。Fix：所有 fallback 容量流行统一 `order_exempt=true`（排除阅读序门、计入零丢失、报告单列），或明确部分豁免的判定依据。
- 次要：S5 与 S6 的 verify 输入描述不一致（应同时消费 align_out/boxes.json 与 align_report.json）；`make_searchable` 完整新签名未给（含 out_pdf/boxes 位置与旧调用兼容示例）。

### Reviewer final line
VERDICT: REVISE

### Deadlock status（Main agent）
- 已执行 5 轮独立子代理审阅，全部 REVISE；达到默认 MAX_ROUNDS=5。
- 主代理判断：R5 的三个 P0 都成立且属于“可测试规格”缺口，不应虚假 APPROVED。
- 待人工裁决：
  1. 授权继续修正并追加轮次（提高 MAX_ROUNDS）；
  2. 接受当前 R5 剩余问题为已知风险，进入人工签核/实施；
  3. 暂停，先修复其他前置条件（如全量普查/试点）再复审。
