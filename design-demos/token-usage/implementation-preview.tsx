import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import TokenUsageStats from '../../web/src/components/TokenUsageStats'
import '../../web/src/forms.css'

function Preview() {
  const [state, setState] = useState('complete')
  const history = [
    { role: 'assistant', runId: 'first', usageSummary: { input_tokens: 6200, output_tokens: 1800 } },
    { role: 'assistant', runId: 'second', usageSummary: { input_tokens: 9760, output_tokens: 2720 } },
  ]
  const messages = state === 'empty' ? [] : state === 'running'
    ? [...history, { role: 'assistant', runId: 'active' }]
    : state === 'unknown' ? [...history, { role: 'assistant', runId: 'legacy' }] : history
  return <main style={{ maxWidth: 1120, margin: '64px auto', padding: '0 12px' }}>
    <h1 style={{ fontSize: 20, fontWeight: 500 }}>B 方案 · 本轮任务与整个会话</h1>
    <p style={{ fontSize: 13, color: '#696761' }}>实际 React 组件与项目样式，以下为隔离的预览数据。</p>
    <div className="rag-workbench"><div className="rag-composer-wrap">
      <div className="rag-composer-context">项目：测试　0 份附件　0 个参考知识库</div>
      <div className="rag-composer">
        <textarea aria-label="任务输入" placeholder="直接提问，或上传/粘贴数据、文档和图片让 Agent 自主分析…" style={{ minHeight: 126 }} />
        <div className="rag-composer-bar" style={{ padding: '0 14px 12px', justifyContent: 'space-between' }}>
          <span>不使用知识库　＋</span><span>MeritModel　·　关闭　　发送</span>
        </div>
      </div>
      <div className="rag-composer-footer">
        <TokenUsageStats messages={messages} running={state === 'running'} />
      </div>
    </div></div>
    <div style={{ display: 'flex', gap: 8, marginTop: 24, flexWrap: 'wrap' }}>
      {Object.entries({ complete: '已完成两轮', unknown: '含未知记录', running: '运行中', empty: '新会话' }).map(([value, label]) => <button key={value} onClick={() => setState(value)} aria-pressed={state === value}>{label}</button>)}
    </div>
  </main>
}

createRoot(document.getElementById('root')!).render(<Preview />)
