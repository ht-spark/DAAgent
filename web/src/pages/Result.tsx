import { useEffect, useState } from 'react'
import { getFigures, getPaper, getRun } from '../api'
import MarkdownContent from '../components/MarkdownContent'

export default function Result({
  runId,
  onBack,
  onHistory,
}: {
  runId: string
  onBack: () => void
  onHistory: () => void
}) {
  const [paper, setPaper] = useState('')
  const [figures, setFigures] = useState<string[]>([])
  const [run, setRun] = useState<any>(null)

  useEffect(() => {
    getRun(runId).then(setRun).catch(() => {})
    getPaper(runId).then(setPaper).catch(() => setPaper('（暂无报告内容）'))
    getFigures(runId).then(setFigures).catch(() => setFigures([]))
  }, [runId])

  const artifacts: string[] = run?.artifacts || []
  const dl = (p: string) => `/api/runs/${runId}/files/${encodeURIComponent(p)}`
  // 产物路径里的最后一段作为下载默认文件名（路径示例：questions/q1/solution.py → solution.py）。
  const basename = (p: string) => {
    const parts = p.split('/').filter(Boolean)
    return parts[parts.length - 1] || p
  }

  // 仅展示：paper.md / paper.docx / 图表 / 解题代码
  const paperDocx = artifacts.find((p) => p === 'paper.docx')
  const codeFiles = artifacts
    .filter((p) => /^questions\/[^/]+\/solution\.py$/.test(p))
    .map((p) => ({ path: p, name: `解题代码（${p.split('/')[1]}）.py` }))
  // 图表文件直接走 figures 子目录；与 listing 端点的路径保持一致。
  const figureUrl = (f: string) => `/api/runs/${runId}/files/figures/${encodeURIComponent(f)}`

  return (
    <div className="form-card">
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <button className="back-link" onClick={onBack}>
          ← 返回新建
        </button>
        <button className="back-link" onClick={onHistory}>
          历史任务
        </button>
      </div>

      <h2>
        结果 <span className={`status-tag ${run?.status}`}>{run?.status}</span>
      </h2>
      {run && (
        <div className="meta-row">
          报告标题：{run.paper_title || '-'} · 审查状态：{run.review_status || '-'}
        </div>
      )}

      <h3 className="result-section-title">报告预览</h3>
      <div className="paper-box">
        <MarkdownContent content={paper} />
      </div>

      <div className="dl-section">
        <div className="dl-group-title">下载产物</div>
        <ul className="dl-list">
          <li>
            <a
              href={dl('paper.md')}
              download="paper.md"
              target="_blank"
              rel="noreferrer"
              title="点击下载 paper.md"
            >
              paper.md
            </a>
          </li>
          {paperDocx && (
            <li>
              <a
                href={dl(paperDocx)}
                download="paper.docx"
                target="_blank"
                rel="noreferrer"
                title="点击下载 paper.docx"
              >
                paper.docx
              </a>
            </li>
          )}
          {codeFiles.map((cf) => (
            <li key={cf.path}>
              <a
                href={dl(cf.path)}
                download={basename(cf.path)}
                target="_blank"
                rel="noreferrer"
                title={`点击下载 ${basename(cf.path)}`}
              >
                {cf.name}
              </a>
            </li>
          ))}
        </ul>
      </div>

      <h3 className="result-section-title">图表</h3>
      <div className="figs">
        {figures.map((f) => (
          <a
            key={f}
            href={figureUrl(f)}
            download={f}
            target="_blank"
            rel="noreferrer"
            className="fig-link"
            title={`点击下载 ${f}`}
          >
            <img src={figureUrl(f)} alt={f} loading="lazy" />
          </a>
        ))}
        {figures.length === 0 && <span className="muted">（无图表）</span>}
      </div>

      {run?.status === 'failed' && <div className="err-box">该任务执行失败：{run.error}</div>}
    </div>
  )
}
