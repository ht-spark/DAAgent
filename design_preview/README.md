# MMAgent 前端设计原型（Design Preview）

一套深色科幻风的高保真静态原型，用于预览"炫酷版"界面效果。
**纯 HTML/CSS/JS，零外部依赖，直接双击任意 HTML 即可在浏览器打开。**

## 页面清单

| 文件 | 内容 | 亮点 |
| ---- | ---- | ---- |
| `index.html` | 平台首页 Hero | Canvas 粒子网络 + 漂浮数学符号、滚动计数、发光特性卡、八步流水线动画 |
| `task.html` | 建模任务实时进度页 | SVG 进度环、阶段时间线自动推进、终端日志打字机流、人机协同介入横幅 |
| `brainstorm.html` | RAG 头脑风暴对话页 | AI 回答流式输出、引用角标 ↔ 右侧检索片段联动高亮、RRF 分数条动画 |
| `dashboard.html` | 数据总览看板 | 双系列趋势面积图、方法分布环形图、活跃时段热力图、最近任务表 |

## 设计语言

- 深空底色 `#05070f` + 品牌 Indigo→Cyan 渐变（延续现有前端 `#4a5bc7` 主色）
- 玻璃拟态卡片、辉光描边、入场渐显动画
- 共享设计令牌在 `assets/shared.css`

## 与正式前端的衔接

确认风格后，可将这些视觉规范迁移到 `web/` 的 React 组件：
1. 把 `shared.css` 的 CSS 变量合并进 `web/src/index.css`
2. Hero 背景粒子 → 抽成 `<HeroCanvas />` 组件
3. task.html 时间线 → 替换 `Progress.tsx` 的 SSE 渲染样式
4. brainstorm.html 气泡/引用 → 替换 `Brainstorm.tsx` 的消息组件
