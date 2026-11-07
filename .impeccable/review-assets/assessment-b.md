# Assessment B：独立检测与浏览器证据

目标：`web/src/App.tsx`，覆盖由该入口主动可达的 `web/src`。本评审独立进行，未读取 Assessment A、历史评审或其结论。slug 为主代理提供的 `web-src-app-tsx`，ignore.md 不存在。仅检查，无 UI 或业务代码改动。

## 确定性检测

只运行一次：`& 'C:\Users\hetao\.agents\skills\impeccable\scripts\impeccable.cmd' detect --json web/src`。实际返回码 **2（发现问题，并非运行失败）**。完整未截断输出保存在 `.impeccable/review-assets/assessment-b-detector.json`。

共 **9 条 warning，5 个规则**；slop 类 5 条，quality 类 4 条，无 error。所有命中均在 `D:\DAAgent\web\src\forms.css`，importedBy 均为 `main.tsx`。

| 规则 ID / 名称 | 数量 | 精确行号 | 上下文核查 |
|---|---:|---|---|
| side-tab / Side-tab accent border | 2 | 2833、5177 | 2833 是 `.api-hint` 的 3px 左边强调线；是提示语义，不能据此认定模板化。5177 是 Markdown `blockquote` 的引用线，是明显的语义误报，应从设计缺陷中排除。 |
| border-accent-on-rounded / Border accent on rounded element | 1 | 6852 | `.rag-approval-card` 的 3px 顶边与 14px 圆角确实并存；这是批准请求的功能强调，不能由静态规则推出视觉冲突。未触发真实请求，未实测运行态。 |
| overused-font / Overused font | 1 | 6395 | `.new-chat-page.rag-agent` 字体栈包含 Inter，语法命中真实。但中文主要依赖后备字体，且未确认设备实际安装/渲染 Inter；单凭字体名称不能证明界面雷同。 |
| bounce-easing / Bounce or elastic easing | 1 | 4844 | 动画名 `progress-dot-bounce` 触发规则；实际 4850–4858 是 opacity 与 scale(0.7→1.1) 的加载点动画，使用 ease-in-out，没有弹簧/回弹曲线。这是规则名推断的误报，不能记成“弹性缓动缺陷”。加载态未运行。 |
| layout-transition / Layout property animation | 4 | 585、4229、7939、8467 | 分别为主内容 margin-left、上传进度 width、项目文件侧栏 width、任务进度 width。确有布局属性过渡；585/7939 应优先考虑对大量聊天内容的布局成本。4229/8467 是受限的进度条，成本与更新频率相关，不能直接声称卡顿。 |

检测器会漏过同一文件中的多行 transition 声明，例如 32–34 的 sidebar width/padding，以及多处 gap/max-width 过渡。因此“4 条布局动画”是此检测器的命中数量，不是全站此类声明总数。所有规则已读邻近源码核查；审查不把 9 条 warning 当作 9 个已证实用户问题。

## 浏览器证据

使用 CUA 原生 `cua.createBrowserTab('iab','http://localhost:5173',{visible:false})` 创建自己的新标签页（tab 1），没有复用用户页或 A 的页面。桌面 viewport 实测 **1280×720**。未启动、重启或停止现有服务；未发送聊天/LLM 请求；未上传、删除、编辑数据或更改设置。

已实际访问并查看截图/AX：

1. NewChat 默认空态：标题“今天想分析什么？”、输入区、知识库/附件/模型入口；document.scrollWidth 与 innerWidth 均 1280，桌面无横向溢出。
2. 任务专家列表：2 个已发布专家、0 个草稿；查看第一个专家详情，读到了有名称的对话框、职责/SOP/交付要求。
3. 技能列表：docx/pdf/skill-creator/xlsx，查看详情与删除操作可见；未点击删除或上传。
4. 知识库维护总览：更新、统计两个入口及其说明可见；未执行转换或上传。
5. 设置的常规页：字体大小、默认终端选项；只打开并关闭，未改值；未进入可能含密钥的 API 编辑页。

截图已通过 CUA 工具输出供查看，未额外导出图片文件。以下实测数据来自只读 DOM/样式读取与真实键盘交互。

### 高优先级、可复核发现

**[P1] 浅灰文字/占位文本低于正常文字 AA 对比度。** 浏览器读取到聊天输入 placeholder 为 rgb(133,132,127)（#85847f），所在 composer 背景 #fdfcfa，按 WCAG 相对亮度公式得到 **3.65:1**；项目标题文字同为 #85847f、侧栏背景 #fbfaf7，得到 **3.59:1**。这些是正常大小文字，低于 4.5:1。位置：`forms.css:6379` 的 `--rag-text-muted`、`forms.css:7073` 的 placeholder 引用；侧栏项目标题样式位于 `forms.css:10091` 后的项目区。建议用通过 4.5:1 的次要文字 token，并审查所有 muted 用途。来源颜色是实测，不是凭截图判断。

技能上传按钮在 `forms.css:7338–7343` 使用 #fff/#948f88，源码计算 **3.21:1**；截图显示正常字号的小按钮。该组颜色是源码证据，未对该按钮单独抓计算样式，建议列为需修复/复核的同类实例。正文 #2b2a28 在 #f7f6f3 上 **13.27:1**，主正文对比度良好。

**[P1] 模态窗未在打开时把焦点移入、关闭时未还给触发器。** 专家详情打开后 activeElement 仍是背景“查看”；设置打开后焦点仍在侧栏“设置”，`closest('[role=dialog]')` 为 false。设置关闭/Escape 后 activeElement 为 BODY。位置：`components/SettingsModal.tsx:39–48`、`pages/TaskExpert.tsx:423–430`、共享 `modalLayer.ts` 的 pushModalLayer/syncDom；后者有陷阱但没有初始聚焦与恢复逻辑。影响键盘/读屏用户对当前上下文的确认。修复应在共用模态层保存触发器、挂载后聚焦标题/首个合适控件、关闭后恢复，并审查嵌套弹窗。不要误写成“完全没有焦点陷阱”：实际专家详情从外部按 Tab 后进入关闭按钮；设置关闭按钮按 Shift+Tab 后回绕到最后的终端 select；Escape 正常只关闭当前设置。这些均是正向实测。

**[P2] 输入区仅靠 placeholder 提示，缺少稳定的可访问名称。** `pages/NewChat.tsx:1899–1911` textarea 无 label、aria-label、aria-labelledby；DOM 实测 labels.length=0、aria-label=null，AX 输出无名 text entry area。placeholder 内容消失后不提供持久标签。建议提供“分析问题或任务说明”的 label/aria-label，并用说明关联上传/粘贴/换行提示。

**[P2] 核心输入工具和项目管理按钮触控热区偏小。** 桌面实际几何值：添加项目/移除项目均 **24×24**、会话操作 **25×25**、打开项目文件 **32×32**、知识库 summary 高 **30**、添加文件/技能/专家按钮 **30×30**、模型 summary 高 **30**、发送按钮 **34×34**（空态禁用）。这些不达到审查所采用的 44×44 推荐热区；这不是自动宣布 WCAG 2.5.8 失败，因为 AA 24px 目标及间距例外需另外核查。建议扩可点击 padding/最小尺寸，避免把图标也放大。位置：`pages/NewChat.tsx:1912–1922`、`1979`、`2232`、`2317` 附近；对应 `forms.css` 的 rag composer 控件、project section action。

**[P2] 全站 token 有基础但两套色彩系统与硬编码并存。** `index.css:11–68` 为全局蓝色 primary/teal、暖底与文字 token；`forms.css:6369–6388` 的聊天区另设局部暖灰 rag token；sidebar 直接写 #fbfaf7，技能列表 `7338–7405` 直接写按钮/危险颜色。维护时容易让同名状态不同、对比度修复只覆盖一个区域。建议把共用 surface/text/action/status 提为语义 token，保留确有必要的局部变量。本次没有发现/测试暗色开关，不将“没有暗色”本身列为缺陷。

### 性能与 bundle 边界

- `App.tsx:1–16` 静态导入各主页面；`NewChat.tsx:54–55` 静态导入 MarkdownContent 与 ChatModeling。尚未按页面拆包。
- 现有 `web/dist/assets/index-Cg3BJEnh.js` 大小 **812,667 字节，gzip 238,833 字节**（本地 gzip 估算）；CSS **240,016 字节**。这是已有构建产物（时间 2026-11-07 14:00:40），本轮没有重新构建，因此不能当作当前源码精确 bundle 预算或真实线上传输量，也未测首屏耗时/LCP。
- 现有最大 elk chunk **1,477,195 字节**；同时 `MarkdownContent.tsx:61` 用 `import('mermaid')` 进行动态加载，这是正向优化，不能把所有 diagram chunk 视为首屏必载。数学渲染相关 react-markdown/remark/rehype 静态依赖值得按需拆分评估。
- 检测到的布局动画可作为优化候选；没有帧时间/性能录制，不能声称已验证掉帧。`forms.css:10328` 对侧栏、content 等提供 reduced-motion 的 transition:none，避免把已有支持漏报。没有全局 0.01ms 动画强杀。

## 未覆盖与误报边界

- **窄屏**：遵从主代理协调要求，为避免共享 IAB viewport 干扰 A，不修改浏览器 viewport。此 B 报告只包含桌面实测，窄屏由主代理随后统一补验。源码确有 768/640 等断点，但不把存在媒体查询等同“移动端正常”。
- **触摸手势**：未合成 touch、未用物理设备。两条 resize surface 源码有 Pointer Events、pointer capture、pointercancel 与 `touch-action:none`（Sidebar.tsx:290–375；forms.css:51/7949），这优于鼠标专用实现；不能因此声称中断手势和真实触屏已通过。
- **运行/结果态**：未发模型请求，未实测执行进度、命令审批、长答案、图表、报告、错误恢复或流式状态播报；对这些区域仅能使用静态证据。
- **读屏/缩放**：没有运行 NVDA/VoiceOver、200% 文本缩放；AX 和焦点行为不等于完整读屏验收。
- **Overlay**：工具文档明确 `tab.playwright.evaluate` 只读；可写 evaluate/脚本注入 API 未提供，因此没有尝试绕过，没有修改 document.title 或插入 script，没有启动 detector live-server。无页面 overlay、无 impeccable 浏览器控制台检测结果；替代证据是 CLI 全量 JSON、源码逐项核查、截图/AX、只读 computed style 与键盘操作。不能宣称已注入或用户可见 overlay。
- 一次 `body.press('Escape')` 因不可聚焦定位操作超时；改对当前可聚焦“关闭设置”按钮发送 Escape 后成功验证，无业务状态变化。

## 清理与交付

浏览器保持后台（visible:false），未展示 [Human] 标签页。独立 tab 已明确关闭，随后调用 CUA completion_summary“完成独立桌面浏览检查”。没有修改 viewport，无需复位；未启动新服务，无 live-server 残留；保留现有用户 dev server。报告及 detector JSON 是有意保留的审查证据，无待删除临时文件。未运行 build/test，未进行 UI 修复，未读取其他代理文件。
