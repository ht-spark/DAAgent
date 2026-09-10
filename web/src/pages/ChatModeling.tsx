import { useEffect, useRef, useState } from 'react'
import { confirmBudget, createRun, checkBackendOnline, getRun, getRunMonitor, getPaper, getFigures, renameRun, submitClarification, type RunMonitor } from '../api'
import { loadConfigs, loadActiveId, ApiConfig } from '../apiConfigs'
import { NODE_CN, fmtQid, formatCurrentStep, formatProgressLine, nextProgressQuestionId } from '../progressFormat'

type MsgKind = 'info' | 'progress' | 'error' | 'success' | 'warn' | 'result'

/** 结果附件数据：任务完成后在聊天中展示 */
type ResultData = {
  paperContent: string
  paperDocxUrl?: string
  figures: string[]
  codeFiles: { name: string; url: string }[]
  stats?: { time: number; token: number }
}

/** 预览模态框内容类型 */
type PreviewState = {
  title: string
  kind: 'paper' | 'code' | 'images'
  paperContent?: string
  codeContent?: string
  codeUrl?: string
  images?: string[]
  runId: string
} | null

type ChatMsg = {
  id: number
  role: 'user' | 'assistant'
  text: string
  kind: MsgKind
  ts: number
  result?: ResultData
}

type Phase = 'compose' | 'running' | 'done'
type BudgetPhase = 'initial' | 'question' | 'delivery'

type ProgressCardProps = {
  text: string
  currentStep: string
  running: boolean
  budgetReq: { question_id: string; proposed: Record<string, number>; phase: BudgetPhase } | null
  budgetDraft: Record<string, number>
  budgetBusy: boolean
  onBudgetDraftChange: (key: string, value: number) => void
  onBudgetSubmit: (useDefaults: boolean) => void
  clarificationReq: ClarificationReq | null
  clarificationFiles: File[]
  clarificationBusy: boolean
  onClarificationFilesChange: (files: File[]) => void
  onClarificationSubmit: (action: 'terminate' | 'continue') => void
}

function ProgressCard({
  text,
  currentStep,
  running,
  budgetReq,
  budgetDraft,
  budgetBusy,
  onBudgetDraftChange,
  onBudgetSubmit,
  clarificationReq,
  clarificationFiles,
  clarificationBusy,
  onClarificationFilesChange,
  onClarificationSubmit,
}: ProgressCardProps) {
  const completedSteps = text.split('\n').filter(Boolean)

  return (
    <div className="progress-card" aria-live="polite">
      {completedSteps.length > 0 && (
        <div className="progress-history">
          {completedSteps.map((step, index) => (
            <div className="progress-row" key={`${step}-${index}`}>
              <span className="progress-row-mark">·</span>
              <span>{step}</span>
            </div>
          ))}
        </div>
      )}
      <div className={completedSteps.length > 0 ? 'progress-live progress-live-separated' : 'progress-live'}>
        <span className="progress-live-text">
          {running ? `${currentStep || '建模'}进行中` : '建模进度已结束'}
        </span>
        {running && (
          <span className="progress-dots" aria-label="加载中">
            <span></span>
            <span></span>
            <span></span>
          </span>
        )}
      </div>
      {budgetReq && (
        <div className="progress-budget">
          <div className="progress-budget-title">
            {budgetReq.phase === 'initial'
              ? '设置输入信息质量检查预算'
              : budgetReq.phase === 'delivery'
                ? '设置最后交付前质量检查预算'
                : `设置${fmtQid(budgetReq.question_id)}预算`}
          </div>
          <p>
            {budgetReq.phase === 'initial'
              ? '输入信息质量检查未通过时，系统会重做任务理解和输入检查，并消耗一次重试预算。'
              : budgetReq.phase === 'delivery'
                ? '最后交付前质量检查未通过时，系统会修订报告并消耗一次交付修订预算。'
                : '每个子任务独立配置检索、子任务质量检查迭代和代码修复预算；建模方法仍由 LLM 根据问题直接推理。'}
          </p>
          <div className="budget-fields">
            {BUDGET_FIELDS[budgetReq.phase].map((field) => (
              <label key={field.key} className="budget-field">
                <span className="budget-field-label">{field.label}</span>
                <input
                  type="number"
                  min={1}
                  value={budgetDraft[field.key] ?? 0}
                  disabled={budgetBusy}
                  onChange={(event) => onBudgetDraftChange(field.key, Number(event.target.value))}
                />
              </label>
            ))}
          </div>
          <div className="progress-budget-actions">
            <button className="btn-ghost" onClick={() => onBudgetSubmit(true)} disabled={budgetBusy}>
              使用默认
            </button>
            <button className="btn-primary" onClick={() => onBudgetSubmit(false)} disabled={budgetBusy}>
              {budgetBusy ? '提交中…' : '确认覆盖'}
            </button>
          </div>
        </div>
      )}
      {clarificationReq && (
        <div className="progress-clarification">
          <div className="progress-clarification-title">输入材料无法支撑建模</div>
          <p className="progress-clarification-desc">
            输入信息质量检查未通过，当前材料不足以开始可靠建模。请选择终止任务，或上传补充材料后继续。
          </p>
          {clarificationReq.failed_checks.length > 0 && (
            <div className="progress-clarification-checks">
              {clarificationReq.failed_checks.map((c, i) => (
                <span key={i} className="clarification-check-tag">{c}</span>
              ))}
            </div>
          )}
          <div className="progress-clarification-upload">
            <label className={`attach-btn ${clarificationBusy ? 'disabled' : ''}`}>
              📎 上传补充材料
              <input
                type="file"
                multiple
                hidden
                disabled={clarificationBusy}
                onChange={(e) => {
                  const files = Array.from(e.target.files ?? [])
                  if (files.length) onClarificationFilesChange([...clarificationFiles, ...files])
                  e.target.value = ''
                }}
              />
            </label>
            {clarificationFiles.length > 0 && (
              <div className="clarification-file-list">
                {clarificationFiles.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="file-chip">
                    📎 {f.name}
                    <button
                      aria-label="移除"
                      onClick={() => onClarificationFilesChange(clarificationFiles.filter((_, j) => j !== i))}
                      disabled={clarificationBusy}
                    >×</button>
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="progress-clarification-actions">
            <button
              className="btn-danger"
              onClick={() => onClarificationSubmit('terminate')}
              disabled={clarificationBusy}
            >
              材料不足，立即终止
            </button>
            <button
              className="btn-primary"
              onClick={() => onClarificationSubmit('continue')}
              disabled={clarificationBusy || clarificationFiles.length === 0}
            >
              {clarificationBusy ? '提交中…' : '上传补充材料，继续建模'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

const BUDGET_FIELDS: Record<BudgetPhase, { key: string; label: string }[]> = {
  initial: [{ key: 'intake_retry', label: '输入信息质量检查重试次数' }],
  question: [
    { key: 'search', label: '联网检索次数' },
    { key: 'validation', label: '子任务质量检查迭代次数' },
    { key: 'code_repair', label: '代码修复次数' },
  ],
  delivery: [{ key: 'paper_revision', label: '最后交付前质量检查修订次数' }],
}

type BudgetReq = {
  question_id: string
  proposed: Record<string, number>
  phase: BudgetPhase
}

type ClarificationReq = {
  failed_checks: string[]
}

type TimelineItem = {
  key: string
  title: string
  detail: string
  timestamp?: number
  status: 'done' | 'active'
}

function formatDuration(totalSeconds: number | null): string {
  if (totalSeconds === null || !Number.isFinite(totalSeconds) || totalSeconds < 0) return '--'
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = Math.floor(totalSeconds % 60)
  return hours > 0
    ? `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function formatEventTime(timestamp?: number): string {
  if (!timestamp) return ''
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function buildTimeline(events: any[], phase: Phase, currentStep: string): TimelineItem[] {
  const items: TimelineItem[] = []
  const completed = events.filter((event) => event?.type === 'node')
  for (const [index, event] of completed.entries()) {
    const title = formatProgressLine(event, event.current_question_id || '') || NODE_CN[event.node] || '建模步骤'
    items.push({
      key: `${event.node || 'node'}-${event.timestamp || index}`,
      title,
      detail: event.workflow_status ? `状态：${event.workflow_status}` : '步骤已完成',
      timestamp: event.timestamp,
      status: 'done',
    })
  }
  const lastStart = [...events].reverse().find((event) => event?.type === 'node_start')
  const lastCompleted = completed[completed.length - 1]
  if (phase === 'running' && lastStart && lastStart.node !== lastCompleted?.node) {
    items.push({
      key: `active-${lastStart.node}-${lastStart.timestamp || 'now'}`,
      title: formatCurrentStep(lastStart, lastStart.current_question_id || ''),
      detail: '正在执行，新的节点结果会实时同步到此处',
      timestamp: lastStart.timestamp,
      status: 'active',
    })
  } else if (phase === 'running' && items.length === 0) {
    items.push({
      key: 'active-initial',
      title: currentStep || '接收任务',
      detail: '正在准备任务环境与输入材料',
      status: 'active',
    })
  }
  return items.slice(-12)
}

function RunMonitorPanel({
  run,
  monitor,
  log,
  now,
  phase,
  currentStep,
}: {
  run: any | null
  monitor: RunMonitor | null
  log: string
  now: number
  phase: Phase
  currentStep: string
}) {
  const startedAt = run?.created_at ? Date.parse(run.created_at) : NaN
  // 任务结束后应冻结计时：以前端 `now` 计算会让计时器持续上涨，
  // 改为用服务端的 `updated_at` 作为冻结的结束时间戳。
  const endedAt = run?.updated_at ? Date.parse(run.updated_at) : NaN
  const referenceTime = phase === 'done' && Number.isFinite(endedAt) ? endedAt : now
  const elapsed = Number.isFinite(startedAt)
    ? Math.max(0, Math.floor((referenceTime - startedAt) / 1000))
    : null
  const timeline = buildTimeline(run?.progress || [], phase, currentStep)
  const logLines = log.split(/\r?\n/).filter(Boolean).slice(-80)

  return (
    <aside className="run-monitor" aria-label="任务运行监控">
      <section className="run-overview">
        <div className="run-panel-heading">
          <span>◆ 总体进度</span>
          <span className={`run-live ${phase === 'running' ? 'is-live' : ''}`}>{phase === 'running' ? 'LIVE' : run ? '已结束' : '等待任务'}</span>
        </div>
        {run ? (
          <div className="run-overview-content">
            <div className="run-time-orbit"><span>已运行</span><strong>{formatDuration(elapsed)}</strong></div>
            <dl className="run-metrics">
              <div><dt>Token 消耗</dt><dd>{monitor?.token_total === null || monitor?.token_total === undefined ? '--' : monitor.token_total.toLocaleString()}</dd></div>
              <div><dt>LLM 耗时</dt><dd>{formatDuration(monitor?.time_total ?? null)}</dd></div>
            </dl>
          </div>
        ) : (
          <p className="run-empty-copy">开始建模后，会在这里显示时间、Token 与阶段进度。</p>
        )}
        {run && <p className="run-refresh-note">每 30 秒刷新资源统计</p>}
      </section>

      <section className="run-timeline-panel">
        <div className="run-panel-heading"><span>⌁ 求解流水线</span><span>{timeline.length} 个节点</span></div>
        <div className="run-timeline">
          {timeline.length > 0 ? timeline.map((item) => (
            <div className={`run-stage is-${item.status}`} key={item.key}>
              <div className="run-stage-rail"><span>{item.status === 'done' ? '✓' : '·'}</span></div>
              <div className="run-stage-main"><div className="run-stage-title">{item.title}<em>{item.status === 'done' ? '已完成' : '进行中'}</em></div><p>{item.detail}</p></div>
              <time>{formatEventTime(item.timestamp)}</time>
            </div>
          )) : <p className="run-empty-copy">尚未产生可展示的任务节点。</p>}
        </div>
      </section>

      <section className="run-log-panel">
        <div className="run-log-head"><span><i></i><i></i><i></i> run.log</span><b>{phase === 'running' ? '● LIVE' : '记录'}</b></div>
        <pre className="run-log-content">{logLines.length ? logLines.join('\n') : '等待运行日志写入…'}</pre>
      </section>
    </aside>
  )
}

function TaskWorkflow({
  runId,
  taskName,
  run,
  monitor,
  now,
  phase,
  currentStep,
  activeName,
  model,
  onViewResult,
}: {
  runId: string
  taskName: string
  run: any | null
  monitor: RunMonitor | null
  now: number
  phase: Phase
  currentStep: string
  activeName: string
  model?: string
  onViewResult: () => void
}) {
  const timeline = buildTimeline(run?.progress || [], phase, currentStep)
  const startedAt = run?.created_at ? Date.parse(run.created_at) : NaN
  // 任务结束后应冻结计时：用服务端 updated_at 作为结束时间，避免前端 `now` 继续走导致数字持续上涨。
  const endedAt = run?.updated_at ? Date.parse(run.updated_at) : NaN
  const referenceTime = phase === 'done' && Number.isFinite(endedAt) ? endedAt : now
  const elapsed = Number.isFinite(startedAt) ? Math.max(0, Math.floor((referenceTime - startedAt) / 1000)) : null
  const inputFiles = (run?.artifacts || []).filter((path: string) => path.startsWith('input/'))
  const resultFiles = (run?.artifacts || []).filter((path: string) => !path.startsWith('input/'))
  const completedCount = timeline.filter((item) => item.status === 'done').length

  return (
    <main className="workflow-workspace">
      <header className="workflow-header">
        <div>
          <p className="workflow-path">建模任务 / {phase === 'running' ? '运行中' : '已结束'} / {runId}</p>
          <h1>{taskName || run?.task_name || '未命名建模任务'}</h1>
        </div>
        <div className="workflow-actions">
          {phase === 'running' && <span className="workflow-running">运行中</span>}
          {phase === 'done' && <button className="workflow-result-button" onClick={onViewResult}>查看报告</button>}
        </div>
      </header>

      <div className="workflow-grid">
        <aside className="workflow-sidebar">
          <section className="workflow-card workflow-overview-card">
            <h2>总体进度</h2>
            <div className="workflow-overview-body">
              <div className="workflow-orbit"><span>{phase === 'running' ? '处理中' : '完成'}</span><strong>{completedCount}</strong><small>个步骤</small></div>
              <dl>
                <div><dt>已用时间</dt><dd>{formatDuration(elapsed)}</dd></div>
                <div><dt>Token 消耗</dt><dd>{monitor?.token_total === null || monitor?.token_total === undefined ? '--' : monitor.token_total.toLocaleString()}</dd></div>
                <div><dt>模型耗时</dt><dd>{formatDuration(monitor?.time_total ?? null)}</dd></div>
              </dl>
            </div>
            <p className="workflow-refresh-note">资源统计每 30 秒刷新</p>
          </section>

          <section className="workflow-card">
            <h2>模型配置</h2>
            <dl className="workflow-details">
              <div><dt>当前 API</dt><dd>{activeName || '未命名配置'}</dd></div>
              <div><dt>主模型</dt><dd>{model || '默认模型'}</dd></div>
              <div><dt>任务编号</dt><dd className="workflow-code">{runId}</dd></div>
            </dl>
          </section>

          <section className="workflow-card">
            <h2>输入材料</h2>
            {inputFiles.length > 0 ? (
              <ul className="workflow-files">
                {inputFiles.map((path: string) => <li key={path}>{path.replace(/^input\//, '')}</li>)}
              </ul>
            ) : <p className="workflow-empty">正在整理已上传的任务与附件。</p>}
          </section>

          {resultFiles.length > 0 && (
            <section className="workflow-card">
              <h2>实时产物</h2>
              <ul className="workflow-files">{resultFiles.slice(-5).map((path: string) => <li key={path}>{path}</li>)}</ul>
            </section>
          )}
        </aside>

        <section className="workflow-canvas">
          <div className="workflow-canvas-head">
            <div><h2>建模工作流</h2><p>{phase === 'running' ? '节点状态会随任务执行实时更新。' : '任务已结束，可查看最终报告。'}</p></div>
            <span>{phase === 'running' ? '实时状态' : '执行记录'}</span>
          </div>
          <div className="workflow-stages" aria-live="polite">
            {timeline.length ? timeline.map((item) => (
              <article className={`workflow-stage is-${item.status}`} key={item.key}>
                <div className="workflow-stage-marker" aria-hidden="true"><span>{item.status === 'done' ? '✓' : '…'}</span></div>
                <div className="workflow-stage-content"><h3>{item.title}</h3><p>{item.detail}</p></div>
                <time>{formatEventTime(item.timestamp) || (item.status === 'active' ? '进行中' : '')}</time>
              </article>
            )) : (
              <div className="workflow-stage-empty"><strong>正在准备工作流</strong><p>任务节点会在系统开始处理后显示在这里。</p></div>
            )}
          </div>
        </section>
      </div>
    </main>
  )
}

type Props = {
  /** 若提供，则直接进入“接管进行中任务”模式：跳过输入，实时同步该 run 的进度 */
  resumeRunId?: string | null
  /** 建模完成后跳转到结果页 */
  onViewResult: (runId: string) => void
  /** 从灵感讨论带入的预填任务文本 */
  initialProblem?: string
}

let _seq = 1
const nextId = () => _seq++

export default function ChatModeling({ resumeRunId, onViewResult, initialProblem = '' }: Props) {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [problemText, setProblemText] = useState(initialProblem)
  const [problemFile, setProblemFile] = useState<File | null>(null)
  const [dataFiles, setDataFiles] = useState<File[]>([])
  const [phase, setPhase] = useState<Phase>(resumeRunId ? 'running' : 'compose')
  const [runId, setRunId] = useState<string | null>(resumeRunId ?? null)
  const [busy, setBusy] = useState(false)
  const [activeCfg, setActiveCfg] = useState<ApiConfig | null>(null)
  const [activeName, setActiveName] = useState('')
  const [budgetReq, setBudgetReq] = useState<BudgetReq | null>(null)
  const [budgetDraft, setBudgetDraft] = useState<Record<string, number>>({})
  const [budgetBusy, setBudgetBusy] = useState(false)
  const [clarificationReq, setClarificationReq] = useState<ClarificationReq | null>(null)
  const [clarificationFiles, setClarificationFiles] = useState<File[]>([])
  const [clarificationBusy, setClarificationBusy] = useState(false)
  /** 当前正在执行的步骤描述（用于动画展示） */
  const [currentStep, setCurrentStep] = useState('')
  /** 预览模态框状态 */
  const [preview, setPreview] = useState<PreviewState>(null)
  /** 任务名称（用户可编辑） */
  const [taskName, setTaskName] = useState('')
  /** 任务名称编辑态（输入框值） */
  const [taskNameDraft, setTaskNameDraft] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [runSnapshot, setRunSnapshot] = useState<any | null>(null)
  const [runMonitor, setRunMonitor] = useState<RunMonitor | null>(null)
  const [runLog, setRunLog] = useState('')
  const [monitorNow, setMonitorNow] = useState(Date.now())

  const esRef = useRef<EventSource | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const initRef = useRef(false)
  const statsRef = useRef<{ time: number; token: number } | null>(null)
  const progressMsgIdRef = useRef<number | null>(null)
  const progressQueueRef = useRef<any[]>([])
  const progressTimerRef = useRef<number | null>(null)
  const currentQuestionIdRef = useRef('')
  const autoConfirmedBudgetRef = useRef(new Set<string>())

  function push(role: 'user' | 'assistant', text: string, kind: MsgKind = 'info', result?: ResultData) {
    setMessages((m) => [...m, { id: nextId(), role, text, kind, ts: Date.now(), result }])
  }

  // 读取当前激活的 API 配置（与 Submit 页同一来源）
  useEffect(() => {
    const configs = loadConfigs()
    const activeId = loadActiveId()
    const active = configs.find((c) => c.id === activeId) ?? configs[0] ?? null
    setActiveCfg(active)
    setActiveName(active?.name ?? '')
  }, [])

  // 首条消息 + （接管模式下）加载历史进度并订阅实时流
  // 使用 ref 守卫防止 React.StrictMode 开发模式下双执行导致欢迎消息重复
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    if (resumeRunId) {
      loadHistoryAndResume(resumeRunId)
    } else {
      push(
        'assistant',
        '你好，我是 MMAgent 建模助手。\n请在下方输入任务文本，或用按钮上传「任务文件」；可同时附加若干「数据附件」。准备好后点击「开始建模」，我会把建模进度实时推送到这里。',
        'info',
      )
    }
    return () => {
      esRef.current?.close()
      if (progressTimerRef.current !== null) {
        window.clearTimeout(progressTimerRef.current)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 新消息时滚动到底部
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // 资源数据按 30 秒从服务端拉取；耗时用本地时钟连续更新，避免数字停顿。
  useEffect(() => {
    if (!runId) {
      setRunSnapshot(null)
      setRunMonitor(null)
      setRunLog('')
      return
    }
    let cancelled = false
    const refresh = async () => {
      const [snapshot, monitor] = await Promise.all([
        getRun(runId).catch(() => null),
        getRunMonitor(runId).catch(() => null),
      ])
      if (cancelled) return
      if (snapshot) setRunSnapshot(snapshot)
      if (monitor) setRunMonitor(monitor)
    }
    void refresh()
    const refreshTimer = window.setInterval(() => void refresh(), 30_000)
    const clockTimer = window.setInterval(() => setMonitorNow(Date.now()), 1_000)
    return () => {
      cancelled = true
      window.clearInterval(refreshTimer)
      window.clearInterval(clockTimer)
    }
  }, [runId])

  // 纯工作流模式不展示旧聊天界面的预算确认卡，因此默认放行每个预算关卡，
  // 防止任务在 initial / question / delivery 阶段无限等待人工确认。
  useEffect(() => {
    if (!runId || phase !== 'running' || !runSnapshot?.progress) return
    const events: any[] = runSnapshot.progress
    const request = [...events].reverse().find((event) => event?.type === 'budget_request')
    if (!request) return
    const confirmed = events.some((event) =>
      event?.type === 'budget_confirmed'
      && event.phase === request.phase
      && (event.question_id || '') === (request.question_id || ''),
    )
    const requestKey = `${request.phase || 'question'}:${request.question_id || ''}:${request.timestamp || ''}`
    if (confirmed || autoConfirmedBudgetRef.current.has(requestKey)) return

    autoConfirmedBudgetRef.current.add(requestKey)
    void confirmBudget(runId, {
      question_id: request.question_id || undefined,
      use_defaults: true,
    }).catch((error) => {
      autoConfirmedBudgetRef.current.delete(requestKey)
      setSubmitError(`无法确认默认预算：${error instanceof Error ? error.message : String(error)}`)
    })
  }, [phase, runId, runSnapshot])

  /** 追加一行进度到合并进度消息（不存在则创建） */
  function ensureProgressMessage(): number {
    if (progressMsgIdRef.current !== null) {
      return progressMsgIdRef.current
    }
    const id = nextId()
    progressMsgIdRef.current = id
    setMessages((msgs) => {
      return [...msgs, { id, role: 'assistant' as const, text: '', kind: 'progress' as const, ts: Date.now() }]
    })
    return id
  }

  function appendProgress(line: string) {
    const id = ensureProgressMessage()
    if (!line) return
    setMessages((msgs) =>
      msgs.map((m) =>
        m.id === id ? { ...m, text: m.text ? `${m.text}\n${line}` : line, ts: Date.now() } : m,
      ),
    )
  }

  /** 批量设置进度消息文本（用于加载历史事件） */
  function setProgressText(lines: string[]) {
    const text = lines.join('\n')
    // 先确定消息 ID 并同步写入 ref，确保后续 SSE 事件能立即找到目标消息
    const id = progressMsgIdRef.current ?? nextId()
    progressMsgIdRef.current = id
    setMessages((msgs) => {
      const existing = msgs.find((m) => m.id === id)
      if (existing) {
        return msgs.map((m) =>
          m.id === id ? { ...m, text, ts: Date.now() } : m,
        )
      }
      return [...msgs, { id, role: 'assistant' as const, text, kind: 'progress' as const, ts: Date.now() }]
    })
  }

  /** 处理单个进度事件，返回是否为普通节点事件（非预算类） */
  function handleProgressEvent(ev: any) {
    const qidBefore = currentQuestionIdRef.current
    if (ev.type === 'budget_request') {
      const proposed: Record<string, number> = ev.proposed || {}
      const phase: BudgetPhase = ev.phase === 'initial'
        ? 'initial'
        : ev.phase === 'delivery'
          ? 'delivery'
          : 'question'
      setBudgetReq({ question_id: ev.question_id || '', proposed, phase })
      setBudgetDraft({ ...proposed })
    } else if (ev.type === 'budget_confirmed') {
      setBudgetReq(null)
      const line = formatProgressLine(ev, qidBefore)
      if (line) appendProgress(line)
    } else if (ev.type === 'budget_summary') {
      statsRef.current = {
        time: ev.time_total || 0,
        token: ev.token_total || 0,
      }
    } else if (ev.type === 'clarification_request') {
      setClarificationReq({ failed_checks: ev.failed_checks || [] })
      setClarificationFiles([])
    } else if (ev.type === 'clarification_resolved') {
      setClarificationReq(null)
      setClarificationFiles([])
      if (ev.action === 'terminate') {
        appendProgress('用户选择终止建模（材料不足）')
      } else if (ev.action === 'continue') {
        appendProgress('用户上传补充材料，继续建模')
      } else {
        appendProgress('人工澄清已取消')
      }
    } else if (ev.type === 'node_start') {
      // 节点开始事件：只更新动画步骤描述，不追加进度行
      // 如果还没有进度消息，先创建一个空消息让动画立即可见
      setCurrentStep(formatCurrentStep(ev, qidBefore))
      if (progressMsgIdRef.current === null) {
        ensureProgressMessage()
      }
    } else {
      if (ev.type !== 'node') return
      // 普通节点进度（节点完成）：追加到合并进度消息
      const line = formatProgressLine(ev, qidBefore)
      currentQuestionIdRef.current = nextProgressQuestionId(ev, qidBefore)
      if (line) appendProgress(line)
      return
    }
    currentQuestionIdRef.current = nextProgressQuestionId(ev, qidBefore)
  }

  /** 按顺序播放收到的节点事件，避免快步骤在同一帧内跳过。 */
  function drainProgressQueue() {
    const nextEvent = progressQueueRef.current.shift()
    if (!nextEvent) {
      progressTimerRef.current = null
      return
    }
    handleProgressEvent(nextEvent)
    progressTimerRef.current = window.setTimeout(drainProgressQueue, 180)
  }

  function queueProgressEvent(event: any) {
    progressQueueRef.current.push(event)
    if (progressTimerRef.current === null) {
      drainProgressQueue()
    }
  }

  function finishAfterProgressEvents(id: string, status: string, error?: string) {
    const finish = () => {
      if (progressQueueRef.current.length > 0 || progressTimerRef.current !== null) {
        window.setTimeout(finish, 80)
        return
      }
      setPhase('done')
      setCurrentStep('')
      if (status === 'succeeded') {
        push('assistant', '✅ 建模完成！正在加载结果…', 'success')
        loadResults(id)
      } else if (status === 'cancelled') {
        push('assistant', '⚠️ 任务已被中断。', 'warn')
      } else {
        push('assistant', `❌ 建模失败：${error || '未知错误'}`, 'error')
      }
    }
    finish()
  }

  /** 恢复模式：先加载历史进度事件，再接管实时流 */
  async function loadHistoryAndResume(id: string) {
    try {
      const run = await getRun(id)
      if (!run) {
        push('assistant', '未找到该任务记录。', 'error')
        return
      }

      // 恢复模式下从后端加载任务名称
      if (run.task_name) {
        setTaskName(run.task_name)
        setTaskNameDraft(run.task_name)
      }

      const events: any[] = run.progress || []
      const status: string = run.status

      // 将历史事件渲染为进度消息
      const lines: string[] = []
      let lastNode = ''
      let lastQuestionId = ''
      let pendingBudget: BudgetReq | null = null
      let pendingClarification: ClarificationReq | null = null
      for (const ev of events) {
        if (ev.type === 'budget_summary') {
          statsRef.current = {
            time: ev.time_total || 0,
            token: ev.token_total || 0,
          }
        } else if (ev.type === 'budget_request') {
          pendingBudget = {
            question_id: ev.question_id || '',
            proposed: ev.proposed || {},
            phase: ev.phase === 'initial'
              ? 'initial'
              : ev.phase === 'delivery'
                ? 'delivery'
                : 'question',
          }
        } else if (ev.type === 'budget_confirmed') {
          pendingBudget = null
          const line = formatProgressLine(ev, lastQuestionId)
          if (line) lines.push(line)
        } else if (ev.type === 'clarification_request') {
          pendingClarification = { failed_checks: ev.failed_checks || [] }
        } else if (ev.type === 'clarification_resolved') {
          pendingClarification = null
          const line = formatProgressLine(ev, lastQuestionId)
          if (line) lines.push(line)
        } else if (ev.type === 'node_start') {
          // 节点开始事件：只更新最后节点名，不追加进度行
          lastNode = ev.node || 'step'
        } else {
          const line = formatProgressLine(ev, lastQuestionId)
          if (line) lines.push(line)
          lastNode = ev.node || lastNode
        }
        lastQuestionId = nextProgressQuestionId(ev, lastQuestionId)
      }
      currentQuestionIdRef.current = lastQuestionId
      setProgressText(lines)
      setCurrentStep(lastNode ? formatCurrentStep({ node: lastNode }, lastQuestionId) : '')
      if (pendingBudget && (status === 'queued' || status === 'running')) {
        setBudgetReq(pendingBudget)
        setBudgetDraft({ ...pendingBudget.proposed })
      }
      if (pendingClarification && (status === 'queued' || status === 'running')) {
        setClarificationReq(pendingClarification)
      }

      // 根据任务状态决定后续行为
      if (status === 'succeeded') {
        setPhase('done')
        push('assistant', '✅ 建模完成！正在加载结果…', 'success')
        loadResults(id)
      } else if (status === 'failed') {
        setPhase('done')
        push('assistant', `❌ 建模失败：${run.error || '未知错误'}`, 'error')
      } else if (status === 'cancelled') {
        setPhase('done')
        push('assistant', '⚠️ 任务已被中断。', 'warn')
      } else {
        // 仍在运行：接管实时流，跳过已加载的事件
        attachStream(id, events.length)
      }
    } catch {
      // 降级：直接从头订阅 SSE
      push('assistant', '历史进度加载失败，直接订阅实时流…', 'warn')
      attachStream(id, 0)
    }
  }

  function attachStream(id: string, after: number = 0) {
    esRef.current?.close()
    // 仅当从头订阅时重置合并消息；恢复模式（after>0）保留已有进度消息
    if (after === 0) {
      progressMsgIdRef.current = null
      currentQuestionIdRef.current = ''
      progressQueueRef.current = []
      if (progressTimerRef.current !== null) {
        window.clearTimeout(progressTimerRef.current)
        progressTimerRef.current = null
      }
      // 立即创建进度消息，让用户马上看到"正在建模"动画
      ensureProgressMessage()
      setCurrentStep('数据摄入')
    }

    // 跟踪已接收事件数，用于断线重连时携带 after 参数避免重复
    let eventsReceived = after
    let reconnectCount = 0

    const connect = () => {
      const url = eventsReceived > 0
        ? `/api/runs/${id}/progress/stream?after=${eventsReceived}`
        : `/api/runs/${id}/progress/stream`
      const es = new EventSource(url)
      esRef.current = es

      es.onopen = () => {
        reconnectCount = 0
      }

      es.onmessage = (e) => {
        let data: any
        try {
          data = JSON.parse(e.data)
        } catch {
          return
        }
        if (data.type === 'event') {
          eventsReceived++
          const event = data.event || {}
          if (event.type === 'node_start' || event.type === 'node') {
            queueProgressEvent(event)
          } else {
            handleProgressEvent(event)
          }
        } else if (data.type === 'done') {
          es.close()
          finishAfterProgressEvents(id, data.status, data.error)
        } else if (data.type === 'error') {
          push('assistant', `❌ ${data.message || '进度流出错'}`, 'error')
        }
        // 'subscribed' / 注释心跳无需处理
      }

      es.onerror = () => {
        // 关闭当前连接，手动重连以携带 after 参数避免事件重复
        es.close()
        reconnectCount++
        if (reconnectCount === 3) {
          push('assistant', '⚠️ 进度流连接不稳定，正在自动重连…', 'warn')
        }
        if (reconnectCount > 10) {
          push('assistant', '❌ 进度流连接失败，请检查后端服务是否正常运行。', 'error')
          return
        }
        // 指数退避重连（1s → 1.5s → 2.25s … 上限 10s）
        const delay = Math.min(1000 * Math.pow(1.5, reconnectCount - 1), 10000)
        window.setTimeout(() => connect(), delay)
      }
    }

    connect()
  }

  /** 任务完成后：拉取产物并在聊天中展示（仅 paper.md/docx、图片、解题代码） */
  async function loadResults(rid: string) {
    try {
      const [run, paper, figs] = await Promise.all([
        getRun(rid).catch(() => null),
        getPaper(rid).catch(() => ''),
        getFigures(rid).catch(() => []),
      ])

      const artifacts: string[] = run?.artifacts || []
      const dl = (p: string) => `/api/runs/${rid}/files/${encodeURIComponent(p)}`

      // 筛选解题代码：questions/<qid>/solution.py
      const codeFiles = artifacts
        .filter((p) => /^questions\/[^/]+\/solution\.py$/.test(p))
        .map((p) => {
          const qid = p.split('/')[1]
          return { name: `解题代码（${fmtQid(qid)}）.py`, url: dl(p) }
        })

      const paperDocx = artifacts.find((p) => p === 'paper.docx')

      const stats = statsRef.current

      push(
        'assistant',
        '建模结果如下：',
        'result',
        {
          paperContent: paper || '（暂无报告内容）',
          paperDocxUrl: paperDocx ? dl(paperDocx) : undefined,
          figures: figs,
          codeFiles,
          stats: stats ? { time: Math.round(stats.time), token: stats.token } : undefined,
        },
      )
    } catch {
      push('assistant', '结果加载失败，可点击右上角「查看结果」手动查看。', 'warn')
    }
  }

  async function handleStart() {
    if (busy || phase !== 'compose') return
    const text = problemText.trim()
    setSubmitError('')
    if (!text && !problemFile) {
      setSubmitError('请先输入任务文本，或上传任务文件，再开始建模。')
      return
    }
    if (!activeCfg) {
      setSubmitError('尚未配置 API，请先到 API 管理添加并设为默认。')
      return
    }
    setBusy(true)

    // 提交前检查后端是否在线，避免长时间等待后才发现连接失败
    const online = await checkBackendOnline()
    if (!online) {
      setSubmitError('后端服务未启动或无法连接，请检查本地服务。')
      setBusy(false)
      return
    }

    // 用户消息：汇总提交内容
    const parts: string[] = []
    if (problemFile) parts.push(`任务文件：${problemFile.name}`)
    if (text) parts.push(`任务：${text.length > 120 ? text.slice(0, 120) + '…' : text}`)
    if (dataFiles.length) parts.push(`数据附件 ${dataFiles.length} 个：${dataFiles.map((f) => f.name).join('、')}`)
    push('user', parts.join('\n'))

    const fd = new FormData()
    if (problemFile) fd.append('problem_file', problemFile)
    if (text) fd.append('problem_text', text)
    dataFiles.forEach((f) => fd.append('data_files', f))
    const cfg: Record<string, string | number> = { provider: activeCfg.provider, api_key: activeCfg.apiKey }
    if (activeCfg.baseUrl) cfg.base_url = activeCfg.baseUrl
    if (activeCfg.model) cfg.model = activeCfg.model
    if (Number.isInteger(activeCfg.maxTokens) && activeCfg.maxTokens! > 0) cfg.max_tokens = activeCfg.maxTokens!
    if (typeof activeCfg.temperature === 'number' && activeCfg.temperature >= 0 && activeCfg.temperature <= 2) {
      cfg.temperature = activeCfg.temperature
    }
    fd.append('llm_config', JSON.stringify(cfg))
    if (taskName.trim()) fd.append('task_name', taskName.trim())

    try {
      const r = await createRun(fd)
      setRunId(r.run_id)
      setPhase('running')
      setCurrentStep('数据摄入')
      push('assistant', `已收到任务，开始建模。(任务编号${r.run_id})`, 'info')
      attachStream(r.run_id)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setSubmitError(`创建任务失败：${message}`)
    } finally {
      setBusy(false)
    }
  }

  const composing = phase === 'compose'

  async function submitBudget(useDefaults: boolean) {
    if (!runId || !budgetReq || budgetBusy) return
    setBudgetBusy(true)
    const limits: Record<string, number> = {}
    if (!useDefaults) {
      for (const field of BUDGET_FIELDS[budgetReq.phase]) {
        const v = Number(budgetDraft[field.key])
        if (Number.isFinite(v) && v > 0) limits[field.key] = Math.floor(v)
      }
    }
    try {
      await confirmBudget(runId, {
        question_id: budgetReq.question_id,
        use_defaults: useDefaults,
        limits: useDefaults ? undefined : limits,
      })
      setBudgetReq(null) // 服务端随后会推 budget_confirmed 事件补充消息
    } catch (err) {
      push('assistant', `预算确认失败：${err instanceof Error ? err.message : String(err)}`, 'error')
    } finally {
      setBudgetBusy(false)
    }
  }

  async function handleClarification(action: 'terminate' | 'continue') {
    if (!runId || !clarificationReq || clarificationBusy) return
    if (action === 'continue' && clarificationFiles.length === 0) {
      push('assistant', '请先上传补充材料后再继续建模。', 'warn')
      return
    }
    setClarificationBusy(true)
    try {
      await submitClarification(runId, action, action === 'continue' ? clarificationFiles : undefined)
      setClarificationReq(null) // 服务端随后会推 clarification_resolved 事件补充消息
      setClarificationFiles([])
    } catch (err) {
      push('assistant', `澄清提交失败：${err instanceof Error ? err.message : String(err)}`, 'error')
    } finally {
      setClarificationBusy(false)
    }
  }

  async function saveTaskName() {
    const name = taskNameDraft.trim()
    if (!runId) {
      setTaskName(name)
      setEditingName(false)
      return
    }
    try {
      await renameRun(runId, name)
      setTaskName(name)
    } catch {
      // 重命名失败时仍更新本地显示
      setTaskName(name)
    }
    setEditingName(false)
  }

  if (composing) {
    return (
      <main className="task-setup">
        <header className="task-setup-header">
          <p>新建建模任务</p>
          <h1>提交问题与材料</h1>
          <span>系统会根据任务文本和附件创建完整建模工作流。</span>
        </header>
        <section className="task-setup-form">
          <label className="task-setup-label" htmlFor="task-name">任务名称</label>
          <input id="task-name" className="task-setup-input" value={taskName} onChange={(event) => { setTaskName(event.target.value); setTaskNameDraft(event.target.value) }} placeholder="" />
          <div className="task-upload-grid">
            <label className="task-upload-slot">
              <span>任务文件</span><strong>{problemFile?.name || '选择 Markdown 或 TXT 文件'}</strong>
              <input type="file" accept=".md,.txt" hidden onChange={(event) => { setProblemFile(event.target.files?.[0] ?? null); event.target.value = '' }} />
            </label>
            <label className="task-upload-slot">
              <span>数据附件</span><strong>{dataFiles.length ? `已选择 ${dataFiles.length} 个附件` : '选择数据、表格或图片'}</strong>
              <input type="file" multiple hidden onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) setDataFiles((current) => [...current, ...files]); event.target.value = '' }} />
            </label>
          </div>
          {(problemFile || dataFiles.length > 0) && (
            <div className="task-file-list">
              {problemFile && <span>{problemFile.name}<button onClick={() => setProblemFile(null)} aria-label="移除任务文件">×</button></span>}
              {dataFiles.map((file, index) => <span key={`${file.name}-${index}`}>{file.name}<button onClick={() => setDataFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))} aria-label={`移除 ${file.name}`}>×</button></span>)}
            </div>
          )}
          <div className="task-setup-footer">
            <p className={submitError ? 'task-setup-error' : ''}>{submitError || (activeName ? `将使用 ${activeName}${activeCfg?.model ? ` / ${activeCfg.model}` : ''}` : '请先在 API 管理中设置可用配置')}</p>
            <button className="task-start-button" onClick={handleStart} disabled={busy}>{busy ? '正在创建任务…' : '开始建模'}</button>
          </div>
        </section>
      </main>
    )
  }

  if (runId) {
    return (
      <TaskWorkflow
        runId={runId}
        taskName={taskName}
        run={runSnapshot}
        monitor={runMonitor}
        now={monitorNow}
        phase={phase}
        currentStep={currentStep}
        activeName={activeName}
        model={activeCfg?.model}
        onViewResult={() => onViewResult(runId)}
      />
    )
  }

  return (
    <div className="chatapp">
      <section className="chatapp-conversation">
      <div className="chatapp-head">
        <div>
          <div className="chatapp-title-row">
            {editingName ? (
              <input
                className="task-name-input"
                value={taskNameDraft}
                onChange={(e) => setTaskNameDraft(e.target.value)}
                onBlur={saveTaskName}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); saveTaskName() }
                  if (e.key === 'Escape') { setTaskNameDraft(taskName); setEditingName(false) }
                }}
                placeholder="输入任务名称"
                autoFocus
              />
            ) : (
              <h2
                className="chatapp-title"
                onClick={() => { setTaskNameDraft(taskName); setEditingName(true) }}
                title="点击编辑任务名称"
                style={{ cursor: 'pointer' }}
              >
                {taskName || '建模对话'}
                <span className="chatapp-title-edit">✎</span>
              </h2>
            )}
          </div>
          <div className="chatapp-sub">
            {activeName ? (
              <>
                当前 API：<strong>{activeName}</strong>
                {activeCfg?.model ? ` · ${activeCfg.model}` : ''}
              </>
            ) : (
              '尚未配置 API（请到「API 管理」添加）'
            )}
            {runId && (
              <>
                {' '}· 任务编号 <code>{runId}</code>
              </>
            )}
          </div>
        </div>
        {phase === 'done' && runId && (
          <button className="btn-primary" onClick={() => {
            const lastResult = messages.find(m => m.result)
            if (lastResult) {
              listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
            }
          }}>
            滚动到结果 ↓
          </button>
        )}
      </div>

      <div className="chatapp-messages" ref={listRef}>
        {messages.map((m) => (
          <div key={m.id} className={`message ${m.role === 'user' ? 'user' : ''}`}>
            {m.role === 'assistant' && <div className="msg-avatar">M</div>}
            <div className={`msg-content kind-${m.kind}`}>
              {m.kind === 'progress' ? (
                <ProgressCard
                  text={m.text}
                  currentStep={currentStep}
                  running={phase === 'running'}
                  budgetReq={budgetReq}
                  budgetDraft={budgetDraft}
                  budgetBusy={budgetBusy}
                  onBudgetDraftChange={(key, value) =>
                    setBudgetDraft((draft) => ({ ...draft, [key]: value }))
                  }
                  onBudgetSubmit={submitBudget}
                  clarificationReq={clarificationReq}
                  clarificationFiles={clarificationFiles}
                  clarificationBusy={clarificationBusy}
                  onClarificationFilesChange={setClarificationFiles}
                  onClarificationSubmit={handleClarification}
                />
              ) : (
                <div className="msg-text">{m.text}</div>
              )}
              {m.result && (
                <div className="result-block">
                  {m.result.stats && (
                    <div className="result-stats">
                      <span>⏱ 耗时 {m.result.stats.time} 秒</span>
                      <span>🔤 token {m.result.stats.token.toLocaleString()}</span>
                    </div>
                  )}
                  <div className="result-cards">
                    <button
                      className="result-card"
                      onClick={() => setPreview({
                        title: 'Paper (Markdown)',
                        kind: 'paper',
                        paperContent: m.result!.paperContent,
                        runId: runId!,
                      })}
                    >
                      <span className="result-card-icon">📄</span>
                      <span className="result-card-label">paper.md</span>
                      <span className="result-card-hint">点击预览</span>
                    </button>
                    {m.result.paperDocxUrl ? (
                      <a
                        className="result-card"
                        href={m.result.paperDocxUrl}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span className="result-card-icon">📄</span>
                        <span className="result-card-label">paper.docx</span>
                        <span className="result-card-hint">点击下载</span>
                      </a>
                    ) : (
                      <div className="result-card result-card-disabled">
                        <span className="result-card-icon">📄</span>
                        <span className="result-card-label">paper.docx</span>
                        <span className="result-card-hint">未生成</span>
                      </div>
                    )}
                    {m.result.codeFiles.length > 0 ? (
                      <button
                        className="result-card"
                        onClick={async () => {
                          const cf = m.result!.codeFiles[0]
                          try {
                            const resp = await fetch(cf.url)
                            const code = await resp.text()
                            setPreview({
                              title: cf.name,
                              kind: 'code',
                              codeContent: code,
                              codeUrl: cf.url,
                              runId: runId!,
                            })
                          } catch {
                            setPreview({
                              title: cf.name,
                              kind: 'code',
                              codeContent: '无法加载代码内容',
                              codeUrl: cf.url,
                              runId: runId!,
                            })
                          }
                        }}
                      >
                        <span className="result-card-icon">🐍</span>
                        <span className="result-card-label">解题代码</span>
                        <span className="result-card-hint">点击预览</span>
                      </button>
                    ) : (
                      <div className="result-card result-card-disabled">
                        <span className="result-card-icon">🐍</span>
                        <span className="result-card-label">解题代码</span>
                        <span className="result-card-hint">未生成</span>
                      </div>
                    )}
                    {m.result.figures.length > 0 ? (
                      <button
                        className="result-card"
                        onClick={() => setPreview({
                          title: '图表',
                          kind: 'images',
                          images: m.result!.figures.map((f) =>
                            `/api/runs/${runId}/files/figures/${encodeURIComponent(f)}`,
                          ),
                          runId: runId!,
                        })}
                      >
                        <span className="result-card-icon">🖼️</span>
                        <span className="result-card-label">图表</span>
                        <span className="result-card-hint">{m.result.figures.length} 张 · 点击预览</span>
                      </button>
                    ) : (
                      <div className="result-card result-card-disabled">
                        <span className="result-card-icon">🖼️</span>
                        <span className="result-card-label">图表</span>
                        <span className="result-card-hint">未生成</span>
                      </div>
                    )}
                  </div>
                </div>
              )}
              <div className="msg-time">{new Date(m.ts).toLocaleTimeString()}</div>
            </div>
            {m.role === 'user' && <div className="msg-avatar msg-avatar-user">U</div>}
          </div>
        ))}
      </div>

      <div className="chatapp-composer">
        {(problemFile || dataFiles.length > 0) && (
          <div className="chip-row">
            {problemFile && (
              <span className="file-chip">
                📄 {problemFile.name}
                <button aria-label="移除任务文件" onClick={() => setProblemFile(null)} disabled={!composing}>
                  ×
                </button>
              </span>
            )}
            {dataFiles.map((f, i) => (
              <span key={`${f.name}-${i}`} className="file-chip">
                📎 {f.name}
                <button
                  aria-label="移除数据附件"
                  onClick={() => setDataFiles((d) => d.filter((_, j) => j !== i))}
                  disabled={!composing}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        <textarea
          className="chatapp-textarea"
          placeholder={
            composing
              ? '在此输入任务文本，或用下方按钮上传任务文件 / 数据附件…'
              : phase === 'running'
                ? '建模进行中，完成后可查看结果'
                : '本次建模已结束'
          }
          value={problemText}
          onChange={(e) => setProblemText(e.target.value)}
          disabled={!composing}
          rows={2}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleStart()
            }
          }}
        />

        <div className="chatapp-toolbar">
          <label className={`attach-btn ${composing ? '' : 'disabled'}`}>
            📄 任务文件
            <input
              type="file"
              accept=".md,.txt"
              hidden
              disabled={!composing}
              onChange={(e) => {
                setProblemFile(e.target.files?.[0] ?? null)
                e.target.value = ''
              }}
            />
          </label>
          <label className={`attach-btn ${composing ? '' : 'disabled'}`}>
            📎 数据附件
            <input
              type="file"
              multiple
              hidden
              disabled={!composing}
              onChange={(e) => {
                const files = Array.from(e.target.files ?? [])
                if (files.length) setDataFiles((d) => [...d, ...files])
                e.target.value = ''
              }}
            />
          </label>
          <div className="spacer" />
          <button className="chatapp-send" onClick={handleStart} disabled={busy || !composing}>
            {busy ? '提交中…' : '开始建模 →'}
          </button>
        </div>
      </div>

      </section>
      <RunMonitorPanel
        run={runSnapshot}
        monitor={runMonitor}
        log={runLog}
        now={monitorNow}
        phase={phase}
        currentStep={currentStep}
      />

      {/* 预览模态框 */}
      {preview && (
        <div className="modal-mask" onClick={() => setPreview(null)}>
          <div className="modal preview-modal" onClick={(e) => e.stopPropagation()}>
            <div className="preview-head">
              <h3>{preview.title}</h3>
              <div className="preview-head-actions">
                {preview.kind === 'paper' && (
                  <a
                    className="btn-primary btn-sm"
                    href={`/api/runs/${preview.runId}/files/paper.md`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    下载 paper.md
                  </a>
                )}
                {preview.kind === 'code' && preview.codeUrl && (
                  <a
                    className="btn-primary btn-sm"
                    href={preview.codeUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    下载代码
                  </a>
                )}
                <button className="btn-ghost btn-sm" onClick={() => setPreview(null)}>关闭</button>
              </div>
            </div>
            <div className="preview-body">
              {preview.kind === 'paper' && (
                <pre className="preview-paper">{preview.paperContent}</pre>
              )}
              {preview.kind === 'code' && (
                <pre className="preview-code">{preview.codeContent}</pre>
              )}
              {preview.kind === 'images' && preview.images && (
                <div className="preview-figs">
                  {preview.images.map((src, i) => (
                    <img key={i} src={src} alt={`figure-${i}`} className="preview-fig" />
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
