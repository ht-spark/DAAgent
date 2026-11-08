/** 助手任务的真实用量；旧记录与畸形计量始终视为未知。 */
export type TokenUsageMessage = {
  role: string
  runId?: string
  usageSummary?: unknown
}

export type TokenCounts = {
  input: number | null
  output: number | null
  total: number | null
  knownInput: number
  knownOutput: number
  knownTotal: number
  runs: number
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function readUsage(value: unknown): TokenCounts {
  const data = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {}
  const input = count(data.input_tokens)
  const output = count(data.output_tokens)
  const knownInput = input ?? count(data.known_input_tokens) ?? 0
  const knownOutput = output ?? count(data.known_output_tokens) ?? 0
  return {
    input, output, total: input !== null && output !== null ? input + output : null,
    knownInput, knownOutput, knownTotal: knownInput + knownOutput, runs: 1,
  }
}

/** 汇总当前会话；活动任务在结束前不计入已完成累计，重放不重复计费。 */
export function collectTokenUsage(messages: readonly TokenUsageMessage[], running: boolean): {
  current: TokenCounts | null
  session: TokenCounts
} {
  const assistants = messages.filter(message => message.role === 'assistant')
  const latest = assistants[assistants.length - 1]
  const finished = running ? assistants.slice(0, -1) : assistants
  const unique = new Map<string | number, TokenUsageMessage>()
  finished.forEach((message, index) => {
    const key = message.runId || index
    if (!unique.has(key) || message.usageSummary !== undefined) unique.set(key, message)
  })
  const usages = [...unique.values()].map(message => readUsage(message.usageSummary))
  const knownInput = usages.reduce((sum, usage) => sum + usage.knownInput, 0)
  const knownOutput = usages.reduce((sum, usage) => sum + usage.knownOutput, 0)
  const input = usages.some(usage => usage.input === null) ? null : knownInput
  const output = usages.some(usage => usage.output === null) ? null : knownOutput
  return {
    current: !running && latest ? readUsage(latest.usageSummary) : null,
    session: {
      input, output, total: input !== null && output !== null ? input + output : null,
      knownInput, knownOutput, knownTotal: knownInput + knownOutput, runs: usages.length,
    },
  }
}

/** 缺失时显示未知；已知部分仅作为下界，真实零值保持为零。 */
export function formatTokenCount(value: number | null, known: number): string {
  if (value !== null) return value.toLocaleString('zh-CN')
  return known > 0 ? `≥${known.toLocaleString('zh-CN')}` : '未知'
}
