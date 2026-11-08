"""生成 token 展示的独立 HTML 初稿，不修改生产组件。"""

from __future__ import annotations

import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = Path("C:/Users/hetao/AppData/Local/Temp/codex-clipboard-bad4aa6c-8f3e-481c-86db-019473cb5d44.png")
image = base64.b64encode(SOURCE.read_bytes()).decode("ascii")

style = """
* { box-sizing: border-box; }
body { margin: 0; background: #f7f6f3; color: #303532; font-family: 'Segoe UI','Microsoft YaHei',sans-serif; }
main { max-width: 1320px; margin: 0 auto; padding: 26px 26px 40px; }
header { display:flex; justify-content:space-between; align-items:baseline; gap:20px; margin:0 0 20px; }
h1 { font-size:20px; font-weight:600; margin:0; } header p { font-size:12px; color:#646963; margin:0; }
section { margin:0 0 25px; } h2 { margin:0 0 6px; font-size:14px; font-weight:600; }
h2 span { font-size:12px; font-weight:400; color:#626861; margin-left:12px; }
.surface { position:relative; } .original { width:100%; aspect-ratio:1984 / 310; overflow:hidden; }
.original img { display:block; width:100%; height:auto; }
.footer { width:82.1%; margin:0 auto; padding:0 15px; min-height:44px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.hint { margin:0; font-size:12px; line-height:1.7; color:#656b64; }
.stat { position:relative; flex:0 0 auto; font-size:12px; color:#59635d; }
button { font:inherit; color:inherit; cursor:pointer; } summary { cursor:pointer; list-style:none; min-height:44px; display:flex; gap:7px; align-items:center; }
summary::-webkit-details-marker { display:none; } summary:focus-visible { outline:2px solid #44765f; outline-offset:3px; border-radius:4px; }
.num { font-family:'Segoe UI',sans-serif; font-variant-numeric:tabular-nums; color:#303f36; font-weight:600; }
.info { font-size:11px; border:1px solid #8b948d; color:#67746a; width:13px; height:13px; text-align:center; border-radius:50%; line-height:12px; }
.popover { position:absolute; right:0; bottom:45px; z-index:2; width:242px; padding:17px; border:1px solid #d7ded8; border-radius:12px; background:#fff; box-shadow:0 8px 35px #283e2920; color:#535e56; }
.popover h3 { margin:0 0 14px; color:#303c33; font-size:13px; font-weight:600; }
.row { display:flex; justify-content:space-between; gap:12px; margin:8px 0; font-size:12px; }
.total { border-top:1px solid #e5e9e6; padding-top:10px; margin-top:12px; }
.popover p { font-size:12px; color:#68746c; margin:13px 0 0; line-height:1.6; }
.split { display:flex; align-items:center; gap:15px; white-space:nowrap; }
.split > span { display:flex; gap:5px; } .split .sum { border-left:1px solid #c9d0cb; padding-left:14px; }
.pill { padding:0 12px; gap:10px; border:1px solid #d5dfd7; background:#edf2ee; border-radius:22px; min-height:34px; margin:5px 0; }
.bar { width:32px; height:5px; border-radius:3px; overflow:hidden; display:flex; background:#c7d2c9; }
.bar i { width:78.2%; background:#668a75; } .bar b { flex:1; background:#c7d2c9; }
.line-two { display:grid; text-align:right; gap:4px; padding:7px 0; } .line-two small { font-size:12px; }
.meter { display:inline-block; width:15px; height:12px; background:linear-gradient(to right,#76917f 0 3px,transparent 3px 6px,#76917f 6px 9px,transparent 9px 12px,#76917f 12px); clip-path:polygon(0 66%,20% 66%,20% 100%,40% 100%,40% 33%,60% 33%,60% 100%,80% 100%,80% 0,100% 0,100% 100%,0 100%); }
.tools { margin-top:18px; border-top:1px solid #dce1db; padding-top:14px; display:flex; align-items:center; gap:9px; flex-wrap:wrap; font-size:12px; color:#5e675f; }
.tools button { background:#fff; border:1px solid #d3dbd4; border-radius:6px; min-height:36px; padding:0 12px; }
.tools button[aria-pressed=true] { background:#e4ece5; border-color:#9ab29f; color:#315740; }
.mobile-composer { display:none; } .thumbs { display:grid; grid-template-columns:1fr 1fr; gap:20px; } .thumbs section { min-width:0; } .thumbs .hint { display:none; } .thumbs .footer { justify-content:flex-end; width:100%; }
a { color:#315b43; } .note { margin:8px 0; color:#647066; font-size:12px; line-height:1.6; }
@media(max-width:900px) { main { padding:22px 15px; } header { display:block; } header p { margin-top:8px; } .footer { width:90%; padding:0 8px; } .hint { max-width:55%; } .split { gap:8px; } }
@media(max-width:600px) { header p { line-height:1.7; } h2 span { display:block; margin:5px 0; } .original { display:none; } .mobile-composer { display:block; margin-top:14px; padding:14px; background:#fff; border:1px solid #d8ddd6; border-radius:14px; } .mobile-composer p { font-size:16px; color:#636b65; margin:0 0 45px; } .mobile-composer div { display:flex; justify-content:space-between; color:#546158; font-size:14px; } .mobile-composer b { color:#6c8d79; } .footer { width:100%; flex-wrap:wrap; padding:4px; gap:0; } .hint { max-width:none; width:100%; font-size:12px; margin-top:5px; } .stat { margin-left:auto; } section { margin-bottom:24px; } .thumbs { grid-template-columns:1fr; } }
"""

def content(key: str) -> str:
    """生成同口径、不同信息组织的统计控件。"""
    if key == "B":
        return '<div class="split"><span>输入 <b class="num input">9,760</b></span><span>输出 <b class="num output">2,720</b></span><span class="sum">合计 <b class="num amount">12,480</b></span></div>'
    if key == "E":
        return '<div class="line-two"><span>本轮 <b class="num amount">12,480</b> tokens</span><small>输入 <span class="input">9,760</span> / 输出 <span class="output">2,720</span></small></div>'
    labels = {
        "A": '本轮消耗 <b class="num amount">12,480</b> <span class="unit">tokens</span> <span class="info" aria-hidden="true">i</span>',
        "C": '<span class="bar" aria-hidden="true"><i></i><b></b></span><b class="num amount">12,480</b> <span class="unit">tokens</span>',
        "D": '<b class="num amount">12,480</b> <span class="unit">tokens</span>',
        "F": '<span class="meter" aria-hidden="true"></span>本轮 <b class="num amount">12,480</b> <span class="unit">tokens</span>',
    }
    return f'<details><summary class="{"pill" if key == "C" else ""}" aria-label="查看本轮 token 消耗明细">{labels[key]}</summary><div class="popover"><h3>本轮 token 消耗</h3><div class="row"><span>输入</span><b class="num input">9,760</b></div><div class="row"><span>输出</span><b class="num output">2,720</b></div><div class="row total"><span>合计</span><b class="num amount">12,480</b></div><p>最近一轮运行完成后更新。<br>示例用量，仅供设计预览。</p></div></details>'

names = {"A": "轻量文字", "B": "平铺明细", "C": "分布徽标", "D": "纯数字", "E": "两行账目", "F": "图标与总量"}
descriptions = {"A": "推荐；占用少，点击查看明细", "B": "输入、输出、合计直接可见", "C": "组成条＋总量，点击展开", "D": "备选；极简显示", "E": "备选；两行字段", "F": "备选；增加计量识别点"}

def section(key: str) -> str:
    """用用户原截图保留输入框，仅增加下方右侧展示。"""
    return f'<section data-variant="{key}"><h2>{key}　{names[key]}<span>{descriptions[key]}</span></h2><div class="surface"><div class="original"><img src="data:image/png;base64,{image}" alt="用户提供的现有 DAAgent 输入框" /></div><div class="mobile-composer"><p>直接提问，或上传/粘贴数据、文档和图片让 Agent 自主分析…</p><div><span>不使用知识库　＋</span><span>MeritModel　<b>发送</b></span></div></div><div class="footer"><p class="hint">Enter 发送 · Shift + Enter 换行。附件用于本轮任务，知识库可跨任务复用。</p><div class="stat">{content(key)}</div></div></div></section>'

script = """
document.querySelectorAll('[data-state]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-state]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  const state = button.dataset.state;
  document.querySelectorAll('.amount').forEach(item => item.textContent = state === 'complete' ? '12,480' : state === 'unknown' ? '用量未知' : '统计中…');
  document.querySelectorAll('.unit').forEach(item => item.hidden = state !== 'complete');
  document.querySelectorAll('.input').forEach(item => item.textContent = state === 'complete' ? '9,760' : '—');
  document.querySelectorAll('.output').forEach(item => item.textContent = state === 'complete' ? '2,720' : '—');
  document.querySelectorAll('.bar').forEach(item => item.style.opacity = state === 'complete' ? '1' : '.25');
}));
document.addEventListener('keydown', event => { if(event.key === 'Escape') document.querySelectorAll('details[open]').forEach(item => item.open = false); });
"""

def write_page(filename: str, keys: str, title: str, thumbs: bool = False) -> None:
    """写入可独立打开的初稿，内嵌截图与样式。"""
    controls = '<div class="tools"><span>预览数据状态</span><button data-state="complete" aria-pressed="true">已完成</button><button data-state="unknown" aria-pressed="false">用量未知</button><button data-state="running" aria-pressed="false">统计中</button><span>示例数字，不是当前真实消耗。</span></div>'
    html = f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{style}</style><main><header><h1>{title}</h1><p>原输入框保持不变，新增统计位于下方右侧。示例数据仅用于预览。</p></header><div class="{"thumbs" if thumbs else "variants"}">{"".join(section(key) for key in keys)}</div>{controls}</main><script>{script}</script></html>'
    (ROOT / filename).write_text(html, encoding="utf-8")


write_page("comparison.html", "ABC", "输入框下方 token 展示 · 三个方向")
write_page("thumbs.html", "ABCDEF", "六个候选 · 局部信息组织比较", True)
for index, key in enumerate("ABC", start=1):
    write_page(f"dir-{index}.html", key, f"方案 {key} · {names[key]}")
