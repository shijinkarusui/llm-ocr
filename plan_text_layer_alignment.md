# 双层PDF文字层坐标对齐修复计划

## 0. 背景与根因（已实测）

第一段：现状说明。
现状：make_searchable() 把原页光栅化（200dpi）做底图，文字层两条路：有可用 boxes 走逐行插入，否则走 fallback 均铺。
致命调用：双层页构建函数固定传 boxes=None，全书303页永远走 fallback。fallback 把全文按行宽折行后从页高8%到94%等距均铺，外加底部0.5pt隐形带。这就是复制位置不对、高亮标不了的直接原因。
好消息：原PDF自带可用几何。实测 get_text(words)：303页每页1张整页图，外加16到71个词框；抽 p1/p2/p100 文本是可读中文，garble_ratio 约0.00到0.07。rect坐标可直接用于文字层，无需DPI换算。
boxes生产者当前为零：全仓只有消费端（0-1000归一化加可用性检查），无产出方。CLI 的 boxes-json 入口已存在但无数据可喂。

## 1. 目标与成功标准

目标：同一页内，可复制可搜索可高亮的文字框中心落在对应字形附近，阅读序正确；md高精度文本一个字不丢。
验收门：1）**至少95%的页**满足页内 aligned_coverage≥0.95；未达标页逐一列 fail 原因，且这些页仍须页级非空白字符零丢失；2）可断行溢出率（实测行宽大于框宽）为0，不可断超宽token单列为overflow_expected并warn；3）抽查锚词（正文/IPA/表格各5词，含p21/p100/表10-3）框中心偏差p50不大于25（0-1000系），自动提取文本排序比对通过（order_exempt字符除外），人工框选作抽验；4）verify关键词命中页不变或更好，页级非空白字符零丢失（fallback页单列统计，不混入整体指标）；5）表10-3增加 cell 级 recall/precision 门（预设阈值在实现前由 golden 标定并写入报告），输出 `table_unreliable` 页清单；6）全书303页本地重打通过，无新增第三方重依赖。

## 2. 方案选型结论（你的想法可行，但主次要调）

路线A（主选，零token）：强制对齐。几何取原PDF内嵌词框，md高精度文本做序列对齐后切片到行框，再按框缩字号写入。纯本地difflib加字宽实测，约毫秒每页。
路线B（备选，高成本）：VLM直接回JSON坐标。经llm_client三路vision函数透传可做，但VLM框抖动大（正负5到10%）、口径不稳、表格旋转表易崩、303页token贵且解析脆弱。只作难页补充，不作主路。
路线C（备选，中成本）：本地OCR引擎几何替代内嵌层。只在某页内嵌层缺失或乱码（garble大于约0.3）时启用。新增依赖重，需你拍板（默认不引入，先用A的 fallback 容量流兜底）。
你的原想法（读原PDF文字位置坐标，加AI用高精度md拆JSON加坐标，由脚本替换上去）方向是对的：几何源首选内嵌层（免费现成），AI只在难页补坐标，对齐与替换全本地脚本做。
## 3. 子系统改动分组

### S1 几何抽取（新文件src/geom_extract.py，只用PyMuPDF）
优先使用 `page.get_text("dict")` 的原生 block/line/span bbox，不再用 words 自聚类（避免拆错上标/IPA/表格）；词级 `get_text("words")` 仅用于调试与交叉校验。双栏检测先分栏再排序；竖排窄列标记vertical转置处理。页眉页脚标记header_footer，不丢弃但不参与正文对齐。坐标统一转换到输出坐标系：普查 `/Rotate` 与 cropbox，非0旋转页使用 PyMuPDF `page.rotation_matrix` / `page.derotation_matrix` 与 `page.cropbox` 偏移统一到输出页坐标；无法可靠变换则整页走 fallback 容量流并报告，增加旋转页 golden 测试。模式判定复用garble_ratio（抽为共享函数，阈值0.3用三样本golden标定，不当作拍脑袋常数）：不大于0.3走文本对齐，大于0.3走 fallback 容量流。**校准验收**：固定 golden 页集合为 p1(0-based)/p21/p100/表10-3，普查期输出各页 garble_ratio 分布；若任一 golden 页的实际路径与阈值判定不一致（如 garble 低却被判不可靠、garble 高却被判可对齐），调整阈值或记录为已知例外；最终阈值必须让 4 个 golden 页全部走预期路径，并写入 align_report。**Step 1 全量普查不只是 garble**：对 303 页全部跑 per-line 对齐质量并输出混淆表（garble 判定 vs 实际逐行可对齐率）；若 garble 门预测为 aligned 的页实际不足 95%，则放弃单一 garble 门，改用“逐行失败驱动”的 fallback 容量流（每行独立判定，不再整页一刀切）。`_usable_boxes()` 放宽：单行但有有效几何框也接受；只有无框/坏框才 fallback。

### S2 md归一化（新增_as_text_normalized，旧_as_text不动）
新增 `_as_text_normalized()`：去标题号列表符加粗反引号，链接留字图片删除；GFM表格按行拆格空格连接线性化并保留cell回指；跨行连字符合并；NFC归一保留组合IPA不拆；另存casefold副本比对用。每字符保留回指（md行号，表格cell）定位问题。旧 `_as_text()` 保持原语义继续服务 `_page_boxes()` 的 box 文本清洗与既有 fallback 路径，避免回归。页眉页脚检测规则定义为“位于页顶/页底固定条带 + 正则匹配页码/章节标记/重复跨页特征”三者至少满足其二；**不把页眉页脚从零丢失集合中剔除**——检测到的页眉页脚写入专属 header/footer 几何框并纳入页级零丢失；若某页检测到但无可靠框，则写入 fallback 容量流并在报告中单列“header_footer_excluded 字符数+原因”——**该字段仅表示“未几何对齐、由 fallback 覆盖且计入零丢失”，不表示排除内容**，只是对齐归属分类。页号策略：**内部数据流强制 0-based**（md_dict、boxes、align_report 全部按 pno_0based 索引）；仅在外围兼容层接受 0/1 双键并归一，增补映射测试。不引入未定义术语 `profile`。

### S3 对齐引擎（新文件src/geom_align.py，纯本地）
文本可用页：行级SequenceMatcher求匹配块定锚，块内字符级细分。每行设相似度门限（如ratio≥0.85），低于门限整行走 fallback 容量流并计入失败原因，不强行对齐内嵌 OCR 错行。表格区增加 cell 级匹配路径（cell 回指二分匹配，未匹配 cell 落 fallback 容量流），允许 cell 集合相似度重排容忍列序乱；旋转表用转置文本二次比对，失败整表标不可靠。**cell box 来源**：优先由 `get_text("dict")` 的 span 按行/列聚合为候选 cell，叠加表格线检测（水平/竖线坐标）校正；无法可靠获得 cell 边界时整表走 fallback 容量流并标 `table_unreliable`，不得伪称 cell 级。未匹配的 md 字符不能丢：**固定优先级为“就近行尾 → fallback 容量流 → 专属未匹配盒”**；“专属未匹配盒”定义为：优先把未匹配字符放入**最近的真实行/格几何位置**（保持阅读序）；仅当无任何可用几何时，才使用每页合成的一个 fallback 容量槽（页底边距、整页宽），且该槽内字符标记 `order_exempt=true`——这些字符**仍计入零丢失，但显式排除出阅读序比对门**，避免中页字符被误排到页尾。验收按页校验非空白字符零丢失；阅读序门只对 `order_exempt=false` 的字符生效。**统一术语**：S3 的“容量流”与 S4 的“fallback”指同一条实现路径，全篇统一写作“fallback 容量流”；报告类别只有 `aligned` / `fallback_capacity` / `header_footer_excluded` 三种，不再混用“容量流/fallback”两套名称。文本不可用页：**不声称“真实行位”**，行槽来源明确为 md 行断伪行或等距槽，走 fallback 容量流并单列 `covered-not-aligned`；禁用旧双副本（见S4）。**指标定义**：`aligned_coverage` 的分子为“判定为 aligned 的非空白字符数”，判定规则是“该字符所属行/cell 的 md 块通过相似度门限，且最终插入 bbox 中心与源几何 bbox 中心的偏差 ≤25（0-1000）”；分母为该页非空白字符总数。`covered_coverage` = 页内被任一文字层覆盖（含 fallback 容量流）的非空白字符数 / 该页非空白字符总数；页面算 aligned 需页内 aligned 比例 ≥0.95，报告同时给出行级/页级分布，并输出每字符 aligned/covered 分类。产出 boxes v2 落盘 `out/align/boxes.json` 加 `align_report.json`。**boxes v2 schema 钉死**：顶层为整册 `{page_index_0based: {"reliable": bool, "lines": [{"text": str, "box": [x0,y0,x1,y1], "source": "embedded|md|cells", "mode": "aligned|fallback_capacity|header_footer_excluded", "line_id": str, "column": int, "cell_row": int|null, "cell_col": int|null, "subline_index": int, "order": int, "order_exempt": bool}]}}` 或单页 `{"lines": [...]}`，坐标归一化0-1000且 `x1>x0,y1>y0`，直接兼容 `_page_boxes()`；附单测喂 `_page_boxes()` 验证。`order/column/cell_row/cell_col/subline_index/order_exempt` 是 S5 阅读序交叉校验元数据；`mode` 是 writer 路由与 verify 分类的权威字段：`aligned` 走几何精确插入，`fallback_capacity` 走 fallback 单层，`header_footer_excluded` 走 fallback 覆盖但计零丢失；`_page_boxes()` 必须兼容新增字段（忽略未知字段，不把 fallback_capacity 误判为可靠几何），并加路由单测。**所有 fallback 容量流行（含无几何页 md 伪行/等距槽）统一 `order_exempt=true`**，排除出阅读序门、计入零丢失，并在报告中单列。

### S4 文字层写入（改make_searchable.py，签名兼容）
逐行插入升级：初值取框高0.92倍封顶；水平方向按实测行宽保留原行对齐（居中/左/右），居中时 `x = x0 + (box_w - text_w) / 2`；垂直基线改为用实际插入字体 ascent/descent 校准（`y0 + ascent` 或 `y1 - descent`），不再用 `y0 + fontsize` 经验值。超宽则字号乘框宽除实测宽（只缩不放，下限 `MIN_FONT_PT=4.0`），再超行内折子行不截字，折子行按 step=行高/子行数垂直均布防重叠；**不可断超宽 token（长英文/IPA组合/单元格）不承诺硬性“溢出率=0”**，放入 `overflow_expected` 并打 warn/计报告，可断行仍要求实测不溢出。若框宽小于 `MIN_FONT_PT` 下单个最小字符宽（CJK/ASCII 均可能），判定该行 `overflow_expected` 并优先走 fallback 容量流，不硬算 0。**关键常量写死并写入 align_report**：`SIMILARITY_THRESHOLD=0.85`、`MIN_FONT_PT=4.0`、`MAX_CENTER_DEVIATION=25`、`ALIGNED_PAGE_THRESHOLD=0.95`。render_mode 3加overlay不变。**混排字体规则**：不再整行单选字体；按 span/字符段切分，中文段用 `china-s`、非ASCII段用 `ocripa`、纯ASCII段用 `helv`，组合 IPA 序列不拆；增加“中文+IPA 混排”golden 测试。**布局公式**：每段 `start_x = 行锚点 + Σ(前序段实测宽度)`，段内按该段字体宽度排字；居中/右对齐先在整行按各段总宽计算锚点，再逐段累计；最终行 bbox = 各段插入 bbox 的并集，用于偏差验收与阅读序元数据。**提取规范化**：混排分段插入可能引入字体切换伪影或排序干扰，定义输出 PDF 提取后的 canonicalization——按存储段序列剔除非内容分隔符/重复空格，再与归一化 md 比较；增加专门混排提取单测。`geo_source` 使用明确模式矩阵（四选一，行为确定）：`auto` = 该页 external boxes 存在则用 external，否则 embedded，否则 fallback 容量流；`external` = 该页 external boxes 存在则用 external，否则 fallback 容量流；`embedded` = 忽略外部 boxes，始终内部 geom_extract→align；`fallback_only` = 不读任何几何，直接 fallback 容量流。完整签名：`make_searchable(pdf_path, page_nums_0based, md_dict, boxes_dict_or_None=None, out_pdf=None, geo_source="auto", align_out=None, report=None)`；旧调用 `make_searchable(pdf, pages, md, boxes_or_none, target)` 保持兼容（第4参 boxes、第5参 out_pdf 位置不变）。默认 `auto`；模式与 boxes 参数不匹配时报错（如 `external` 且 boxes=None）。**external 校验链**：即使该页有 external boxes，仍逐页过 `_usable_boxes()` 与逐行相似度门限；external 文件存在但某页空/坏/不可用时，逐页回退 embedded→fallback 容量流，并在 align_report 记录“external_bad→embedded/fallback”原因。fallback 保留作最后兜底但**改为单副本**：保留按行分布的可见位置 8pt 单层（即 fallback 容量流），删除 0.5pt 全文隐藏带，避免 copyable_chars 翻倍；fallback 页同样必须有独立的页级非空白字符零丢失门。字体链不变，但输出报告包含实际可用字体清单，缺失字体页单独降级并 warn。

### S5 验证（扩展verify_searchable.py，只加不改）
新增 `verify_alignment`，接口明确为 `--out-pdf/--md-json/--boxes-json/--align-report/--report`（输出 `verify_report.json`）；**`--boxes-json` 必须消费 `make_searchable` 产出的精确 `align_out/boxes.json`，`--align-report` 必须消费 `align_out/align_report.json`，任一缺失时 fail-fast，不接受任意旧 boxes/report**。输出字段含总页、aligned_coverage、covered_coverage、fallback容量流页（页号+原因）、锚定率、溢出率、overflow_expected、行相似度分布、失败原因分类（低相似度/无框/超宽/不可断超宽/字体缺失/表格失败）、header_footer_excluded统计。验收指标：位置误差（锚词插入框与几何框中心偏差及IoU）、compare_md相似度、关键词高亮抽查；**“框选复制与所见一致”增加自动化检查**：从输出 PDF 提取文本后，**阅读序必须从输出 PDF 的实际 `get_text("dict")` / `get_text("words")` bbox 重新推导**，boxes v2 的 `order/column/cell_row/cell_col/subline_index/order_exempt` 元数据只作交叉校验；排序键为：普通页 `(y, x)`，双栏 `(column_order, y, x)`，竖排按转置后 `(y, x)`，表格按 `(cell_row, cell_col, y, x)`，折子行按 `(归属行, subline_index, x)`；同时增加“原始提取顺序 vs boxes 期望顺序”一致性检查，避免循环验证。`order_exempt=true` 的字符不参与阅读序门，但计入零丢失；先按存储段序列 canonicalize（剔除非内容分隔/字体切换伪影），再把排序后的纯文本与 `_as_text_normalized()` 结果逐页比对，人工框选仅作抽验。字符数验收改为**页级非空白字符差异**，fallback 页单列统计，不再用全书总量≤5%掩盖页级缺失；garble_ratio和self_check保留作模式判定依据。

### S6 界面与打包（薄改）
命令行参数加在 `make_searchable.py` 的独立 main（`--geo-source/--align-out/--report`，其中 `--report` 由 make_searchable 产出 `align_report.json`），暂不扩 unified CLI；`verify_alignment` 只消费 align_report 并输出自己的 `verify_report.json`，两者职责不重叠。如后续统一入口再加并更新 README。图形界面“4 · 双层”页加几何源下拉、进度、报告路径；冒烟覆盖不崩。`llm-ocr-gui.spec` **统一采用 hiddenimports** 列出 `geom_extract`、`geom_align` 及新增模块，并增加 frozen 冒烟测试验证打包后能导入；不再保留“或静态导入”的二选一。无新增依赖（A路线只要PyMuPDF加标准库）；若启用C路线才加依赖重评体积。**实施采用30页试点 + 临时文件/版本号输出 + 验收通过后原子替换**，避免直接覆盖现有 `book_searchable.pdf`。
## 4. 接口与数据流变更
新增extract_words/extract_lines、align_page/align_all、verify_alignment；复用_page_boxes/_usable_boxes契约与check接口；数据流为原PDF词框加页矩与pages下md，经几何抽取、对齐、boxes.json、boxed或fallback写入、book_searchable.pdf、verify_alignment；usage与OCR链不动。align_report含总页、boxed页、fallback页（页号加原因）、覆盖率、锚定率、溢出率。

## 5. 边缘与失败模式
内嵌层缺失、单行页、可用性检查不过走 fallback 容量流并打warn；双栏中缝误切加最小栏宽加抽查；竖排旋转表转置比对加表头锚，失败整表标不可靠走 fallback 容量流；表格线性化丢列序用cell回指校验召回率；IPA组合不拆加下限字号加溢出折行；行聚类阈值参数化加三样本golden；加密缺页缺md空页fail-fast计入报告；B路线JSON失败schema校验加重试1次仍失败回A。

## 6. 测试与验收
单测：归一化、行聚类、容量切片不丢字、缩放上下限、fallback触发。集成：p21纯密排、p100图文混排、表10-3长表、旋转表页，全链跑通。验收即第1节五条，人工框选必过。回归：py_compile全量、冒烟5页签、notation的25例、verify命中不退化。

## 7. 实施顺序（4步，每步可验）
1）S1加S5探针：全书303页普查加三样本golden定阈值，只读半天；2）S2加S3对齐：归一化加行级对齐加容量流产boxes与报告抽查IoU，1天；3）S4写入升级加全书重打（200dpi底不变）加跑分，1天；4）S6界面命令行薄改加冒烟加打包冒烟交付，半天。

## 8. 假设与不做
假设内嵌层坐标可信（已抽样，普查若推翻则切C）、光栅铺满与rect对齐成立、md页号映射正确、arial存在（缺失降级）。联网搜索本次不可用（网关404），VLM框精度按保守估计不作主路依据。不做：不重跑303页OCR、不换光栅DPI、不动batch与usage续跑、不引入重型CV或OCR依赖（除非普查推翻假设并经批准）。

## 9. 审阅修订记录（独立子代理 R1 → 本版）
依据独立子代理 `VERDICT: REVISE`（25条）修订：
- S1：改用原生 dict 行对象；候选字线阈值黄金标定；放宽单行页；处理 /Rotate 与 cropbox。
- S2：新增 `_as_text_normalized()` 隔离旧 `_as_text()`；明确 0-based 页号；页眉页脚同步。
- S3：钉死 boxes v2 schema；无几何页改为 md 伪行/等距并拆 aligned/covered；增加行相似度门限、cell 级匹配、未匹配字符落点；不再声称“真实行位”。
- S4：水平居中/垂直基线用真实度量；折子行均布；不可断超宽走 overflow_expected；明确 geo_source 调用链；fallback 改单副本。
- S5：verify_alignment 接口与报告字段；增加提取文本排序比对；字符验收改页级零丢失。
- S6：CLI 挂载位置明确；spec hiddenimports；30页试点+临时输出+原子替换。
- 部分意见已吸收进验收门第1节。

### R2（独立子代理第二轮 REVISE，11条）修订
- S1：旋转页明确用 rotation_matrix/derotation_matrix + cropbox；增加旋转页 golden。
- S2：页眉页脚检测规则明确（位置+正则+跨页重复 至少二）；0-based 内部统一，移除未定义 `profile`。
- S3：cell box 来源（span 聚合+表格线检测，不可靠走容量流）；aligned/covered 给出分子分母与页级 aligned 条件；未匹配字符固定优先级。
- S4：混排字体按 span/字符段切分并加 golden；fallback 单副本明确为 8pt 行分布单层且 fallback 页仍零丢失；geo_source 给出签名和逐页 external→embedded→fallback 组合。
- S5：提取文本排序键明确（列优先/双栏/竖排/表格 cell/折子行）；verify_alignment 输出 verify_report，与 align_report 职责分离。
- S6：make_searchable 产 align_report、verify_alignment 产 verify_report，避免报告职责重复。

### R3（独立子代理第三轮 REVISE，9条）修订
- S1：garble 阈值校准给出 golden 页集合、期望路径、失败例外规则和 align_report 落盘。
- S2：页眉页脚不再从零丢失集合剔除，写入专属框或报告 header_footer_excluded。
- S3：aligned_coverage 给出逐字符判定规则（行/cell 过阈值 + 插入 bbox 中心偏差≤25）；“专属未匹配盒”定义为页底整宽合成容量槽；统一术语为 fallback 容量流，报告类别固定三种。
- S4：geo_source 改为四模式矩阵（auto/external/embedded/fallback_only），消除逐页组合歧义；混排分段增加提取 canonicalization 与单测。
- S5：verify_alignment 必须消费 align_out/boxes.json，缺失 fail-fast；提取比对加 canonicalization。
- S6：PyInstaller 确定采用 hiddenimports + frozen 冒烟，取消“或静态导入”二选一。

### R4（独立子代理第四轮 REVISE，11条）修订
- S3：boxes v2 增加 `line_id/column/cell_row/cell_col/subline_index/order/order_exempt` 阅读序元数据；未匹配字符优先落到最近真实行/格，合成槽标记 `order_exempt` 并排除出阅读序门但仍计零丢失。
- S4：混排给出逐段 `start_x = 锚点 + Σ前段宽` 和 bbox 并集公式；增加“最小字号单字符宽>框宽”分支；写死 `SIMILARITY_THRESHOLD=0.85/MIN_FONT_PT=4.0/MAX_CENTER_DEVIATION=25/ALIGNED_PAGE_THRESHOLD=0.95` 并写入报告；`auto` 的 external 页仍过可用性与相似度门，坏 external 逐页回退并记录。
- S5：排序键改为普通页 `(y,x)`、双栏 `(column_order,y,x)`、表格 `(cell_row,cell_col,y,x)` 等，优先消费 boxes 元数据；`header_footer_excluded` 语义澄清为已覆盖且计零丢失，仅排除对齐归属。
- 验收门：明确“≥95%的页达标”的聚合断言；增加表10-3 cell 级 recall/precision 门；Step 1 全量 303 页逐行对齐混淆表，必要时改为逐行失败驱动 fallback。

### R5（独立子代理第五轮 REVISE，3 P0 + 2 次要，人工授权后纳入）
- S3：boxes v2 增加 `mode` 字段（aligned/fallback_capacity/header_footer_excluded）作为 writer/verify 路由分类；`_page_boxes()` 兼容未知字段；所有 fallback 容量流行统一 `order_exempt=true`，排除阅读序门但计入零丢失。
- S5：阅读序改为从输出 PDF 实际 bbox 重推，boxes 元数据仅交叉校验；verify 同时消费 `align_out/boxes.json` 与 `align_out/align_report.json`。
- S4：给出完整函数签名并保留旧调用兼容。