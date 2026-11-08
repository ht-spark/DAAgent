import { collectTokenUsage, formatTokenCount, type TokenCounts, type TokenUsageMessage } from '../tokenUsage'

function UsageRow({ label, usage, running = false }: {
  label: string
  usage: TokenCounts | null
  running?: boolean
}) {
  const available = usage !== null && usage.runs > 0
  const title = running
    ? '本轮运行结束后更新实际 token 消耗。'
    : available
      ? '模型服务返回的真实 token 消耗。≥ 表示只统计到部分用量；旧记录或缺失计量不按零计算。'
      : '暂无任务用量。'
  const values = available ? [
    formatTokenCount(usage.input, usage.knownInput),
    formatTokenCount(usage.output, usage.knownOutput),
    formatTokenCount(usage.total, usage.knownTotal),
  ] : ['—', '—', running ? '统计中…' : '—']

  return (
    <div className="rag-token-row" title={title}>
      <span className="rag-token-scope">{label}</span>
      {['输入', '输出', '合计'].map((name, index) => (
        <span className={`rag-token-value${index === 2 ? ' is-total' : ''}`} key={name}>
          <span>{name}</span><strong>{values[index]}</strong>
        </span>
      ))}
    </div>
  )
}

/** 输入框下方的真实消耗：本轮与当前会话分别呈现输入、输出、合计。 */
export default function TokenUsageStats({ messages, running }: {
  messages: readonly TokenUsageMessage[]
  running: boolean
}) {
  const usage = collectTokenUsage(messages, running)
  return (
    <div className="rag-token-usage" role="group" aria-label="Token 消耗统计" aria-live="polite"
      title={running ? '会话累计在本轮运行结束后更新。' : undefined}>
      <UsageRow label="本轮任务" usage={usage.current} running={running} />
      <UsageRow label="整个会话" usage={usage.session} />
      <span className="rag-token-unit">tokens</span>
    </div>
  )
}
