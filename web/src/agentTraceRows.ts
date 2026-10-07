/**
 * Agent 轨迹的「动作 · 摘要」单行列表构造（参考 DeepSeek Harness 的轨迹摘要）。
 *
 * 纯函数、不依赖 DOM：把原始事件流压成一行一步的展示模型，
 * 由 components/AgentTrace 负责渲染。
 *
 * 合并规则：`agent_action` 建立工具行，紧随其后、属于同一次调用的
 * 批准事件与观测事件都并入该行，因此一次工具调用始终只有一行。
 */

import type { AgentTraceEvent } from './api'

/** 轨迹行图标名（由 AgentTrace 映射为具体图形） */
export type TraceIconName =
  | 'start'
  | 'plan'
  | 'think'
  | 'terminal'
  | 'read'
  | 'folder'
  | 'search'
  | 'book'
  | 'globe'
  | 'write'
  | 'edit'
  | 'image'
  | 'data'
  | 'formula'
  | 'convert'
  | 'pause'
  | 'tool'

/** 行状态：只影响配色与动效，不改变行结构 */
export type TraceRowStatus = 'idle' | 'running' | 'success' | 'failed' | 'waiting'

/** 展开后的详情区块 */
export type TraceDetail = {
  label: string
  text: string
}

export type AgentTraceRow = {
  id: string
  icon: TraceIconName
  /** 动作名，如「运行命令」「读取」「思考」 */
  action: string
  /** 单行摘要：命令、路径、查询词或思考首句；与动作名重复时省略 */
  summary?: string
  /** 行尾次要信息：耗时、结果统计、批准状态 */
  meta?: string
  status: TraceRowStatus
  /** 点击行可展开的详细内容；为空表示该行不可展开 */
  details?: TraceDetail[]
  /** 可展开行的展开提示文案 */
  detailLabel?: string
}

/** 单行摘要的最大字符数：超出部分省略，保证每行一行高度 */
const SUMMARY_MAX_LENGTH = 160

/** 工具名 → 图标、动作名与摘要参数优先级 */
const TOOL_PRESENTATION: Record<string, { icon: TraceIconName; action: string; args: string[] }> = {
  execute_python: { icon: 'terminal', action: '运行 Python', args: [] },
  run_terminal_command: { icon: 'terminal', action: '操作终端', args: ['command'] },
  read_workspace_file: { icon: 'read', action: '读取', args: ['relative_path', 'path', 'file_path'] },
  read_attachment_file: { icon: 'read', action: '读取附件', args: ['file_path', 'relative_path', 'path'] },
  edit_attachment_file: { icon: 'edit', action: '原位编辑', args: ['target_path', 'file_path', 'path'] },
  list_workspace_directory: { icon: 'folder', action: '列出目录', args: ['relative_dir', 'path'] },
  list_workspace_files: { icon: 'folder', action: '查找文件', args: ['pattern', 'relative_dir'] },
  search_workspace_text: { icon: 'search', action: '搜索文件内容', args: ['query', 'pattern'] },
  search_knowledge_base: { icon: 'book', action: '检索知识库', args: ['query'] },
  search_web: { icon: 'globe', action: '联网搜索', args: ['query'] },
  write_artifact: { icon: 'write', action: '写入产物', args: ['relative_path', 'path'] },
  write_workspace_file: { icon: 'write', action: '写入文件', args: ['relative_path', 'path'] },
  edit_workspace_file: { icon: 'edit', action: '编辑文件', args: ['relative_path', 'path'] },
  inspect_artifact: { icon: 'read', action: '查看产物', args: ['relative_path', 'path'] },
  inspect_generated_image: { icon: 'image', action: '查看生成的图像', args: ['file_path', 'relative_path'] },
  inspect_image: { icon: 'image', action: '查看图像', args: ['file_path', 'relative_path'] },
  markitdown: { icon: 'convert', action: '转换文档', args: ['file_path', 'relative_path', 'path'] },
  inspect_data_rows: { icon: 'data', action: '查看数据行', args: ['file_name', 'relative_path'] },
  profile_data: { icon: 'data', action: '生成数据画像', args: ['file_name'] },
  explore_math_models: { icon: 'formula', action: '探索数学模型', args: [] },
  retrieve: { icon: 'search', action: '检索', args: ['query'] },
}

/** 取文本第一段有意义的单行内容 */
function firstLine(text: string): string {
  const line = text
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find(Boolean)
  return line ?? ''
}

/** 压成一行摘要：折叠空白并按长度截断 */
export function summarizeText(text: string, maxLength = SUMMARY_MAX_LENGTH): string {
  const collapsed = firstLine(text).replace(/\s+/g, ' ').trim()
  if (collapsed.length <= maxLength) return collapsed
  return `${collapsed.slice(0, maxLength).trimEnd()}…`
}

/** 执行耗时：毫秒转成紧凑可读文本 */
export function formatDuration(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs <= 0) return ''
  if (durationMs < 1000) return `${Math.round(durationMs)} ms`
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)} s`
  const minutes = Math.floor(durationMs / 60_000)
  const seconds = Math.round((durationMs % 60_000) / 1000)
  return `${minutes} 分 ${seconds} 秒`
}

/** 逐字截断过长的错误文本，避免单行摘要被原始堆栈撑满 */
function summarizeError(error: string | null | undefined): string {
  if (!error) return '未提供错误详情'
  const outputSplit = error.split(/(?:---\s*)?(?:执行|运行)输出(?:\s*---)?\s*[:：]?/i, 1)[0].trim()
  const lines = outputSplit.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  // 多行错误里最后一行通常是真正的异常信息，优先展示它
  const core = (lines.length > 1 ? lines[lines.length - 1] : outputSplit) || ''
  return summarizeText(core) || '运行输出过长，已隐藏具体内容'
}

/** 展开详情用的错误原文：保留完整信息，仅防止异常长的输出撑爆视图 */
const ERROR_DETAIL_MAX_LENGTH = 4000

function summarizeErrorDetail(error: string | null | undefined): string {
  const text = (error ?? '').trim()
  if (!text) return '未提供错误详情'
  return text.length <= ERROR_DETAIL_MAX_LENGTH
    ? text
    : `${text.slice(0, ERROR_DETAIL_MAX_LENGTH).trimEnd()}…`
}

/** 观测结果里的统计摘要（只有结构化 summary 才有可读数字） */
function observationStats(event: Extract<AgentTraceEvent, { type: 'agent_observation' }>): string {
  const summary = event.summary
  if (!summary) return ''
  if (typeof summary.match_count === 'number') return `命中 ${summary.match_count} 个片段`
  const missingRate = typeof summary.missing_rate === 'number'
    ? `，缺失率 ${(summary.missing_rate * 100).toFixed(1)}%`
    : ''
  const parts = [
    typeof summary.file_count === 'number' ? `${summary.file_count} 个文件` : '',
    typeof summary.table_count === 'number' ? `${summary.table_count} 张表` : '',
    typeof summary.row_count === 'number' ? `${summary.row_count} 行` : '',
    typeof summary.column_count === 'number' ? `${summary.column_count} 列` : '',
  ].filter(Boolean)
  if (!parts.length) return ''
  return `${parts.join('，')}${missingRate}`
}

/** 从白名单参数与代码里挑出最能说明「这一步在做什么」的摘要 */
function toolSummary(
  toolName: string,
  args: Record<string, string>,
  code: string | undefined,
): string {
  const presentation = TOOL_PRESENTATION[toolName]
  for (const key of presentation?.args ?? []) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) return summarizeText(value)
  }
  if (toolName === 'execute_python' && code?.trim()) {
    // Python 代码的首行注释往往就是这一步的意图，其次才是首行语句
    const comment = code
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find((line) => line.startsWith('#'))
    return summarizeText(comment ? comment.replace(/^#+\s*/, '') : firstLine(code))
  }
  return ''
}

/** 生成模型思考行；无内容时返回 undefined */
function thinkingRow(
  id: string,
  text: string,
  modelReasoning: string | undefined,
  status: TraceRowStatus = 'idle',
): AgentTraceRow | undefined {
  const summary = summarizeText(text)
  if (!summary && status !== 'running') return undefined
  const reasoning = modelReasoning?.trim() ?? ''
  return {
    id,
    icon: 'think',
    action: '思考',
    summary: summary || '正在继续分析…',
    status,
    details: reasoning && reasoning !== text.trim()
      ? [{ label: '模型思考过程', text: reasoning }]
      : undefined,
    detailLabel: '查看本轮思考原文',
  }
}

/**
 * 把轨迹事件压成单行摘要列表。
 *
 * @param trace 完整轨迹事件（含计划与任务生命周期事件）
 * @param options.running 该轨迹是否仍在运行中；为真时最后一行会带上进行中状态
 * @returns 展示用的行列表
 */
export function buildAgentTraceRows(
  trace: AgentTraceEvent[],
  options: { running?: boolean } = {},
): AgentTraceRow[] {
  const running = Boolean(options.running)
  const rows: AgentTraceRow[] = []
  /** 最近一个已发起、尚未收到结果的工具行：后续批准与观测都并入它 */
  let pendingTool: AgentTraceRow | null = null

  trace.forEach((event, index) => {
    const id = `${event.type}-${index}`
    switch (event.type) {
      case 'agent_started': {
        rows.push({
          id,
          icon: 'start',
          action: '开始',
          summary: event.files.length ? `已接收 ${event.files.length} 个附件` : '已接收任务',
          status: 'idle',
        })
        pendingTool = null
        break
      }
      case 'agent_plan': {
        const reasoning = event.reasoning_summary?.trim() ?? ''
        const modelReasoning = event.model_reasoning?.trim() ?? ''
        const details: TraceDetail[] = []
        if (reasoning) details.push({ label: '规划说明', text: reasoning })
        if (modelReasoning) details.push({ label: '模型思考过程', text: modelReasoning })
        if (event.todos.length) {
          details.push({
            label: '任务清单',
            text: event.todos
              .map((todo, order) => `${order + 1}. ${todo.objective}`)
              .join('\n'),
          })
        }
        rows.push({
          id,
          icon: 'plan',
          action: '规划',
          summary: summarizeText(event.objective),
          meta: `${event.todos.length} 个任务`,
          status: 'idle',
          details,
          detailLabel: '查看执行计划',
        })
        break
      }
      case 'agent_action': {
        const thought = (event.thought || event.model_reasoning || '').trim()
        const row = thinkingRow(`${id}-think`, thought, event.model_reasoning)
        if (row) rows.push(row)

        const presentation = TOOL_PRESENTATION[event.tool_name]
        const summary = toolSummary(event.tool_name, event.arguments ?? {}, event.code)
        const tool: AgentTraceRow = {
          id,
          icon: presentation?.icon ?? 'tool',
          action: presentation?.action ?? event.tool_name,
          summary: summary || undefined,
          status: running ? 'running' : 'idle',
          details: event.code?.trim()
            ? [{ label: '本轮 Python 代码', text: event.code.trim() }]
            : undefined,
          detailLabel: '查看本轮代码',
        }
        rows.push(tool)
        pendingTool = tool
        break
      }
      case 'agent_reasoning': {
        const row = thinkingRow(id, event.summary, event.model_reasoning)
        if (row) rows.push(row)
        break
      }
      case 'agent_observation': {
        const target = pendingTool
        pendingTool = null
        if (!target) {
          // 没有对应动作的观测（异常轨迹）：单独成行，避免信息丢失
          rows.push({
            id,
            icon: 'tool',
            action: '执行结果',
            summary: event.success ? observationStats(event) || '执行完成' : summarizeError(event.error),
            meta: formatDuration(event.duration_ms),
            status: event.success ? 'success' : 'failed',
          })
          break
        }
        target.status = event.success ? 'success' : 'failed'
        target.meta = [
          event.success ? '' : '失败',
          observationStats(event),
          formatDuration(event.duration_ms),
        ].filter(Boolean).join(' · ')
        if (!event.success) {
          // 失败时仍以「这一步想做什么」为主摘要，错误信息收进详情，
          // 只有摘要确实为空时才用错误文本兜底。
          const errorText = summarizeError(event.error)
          if (!target.summary) {
            target.summary = errorText
          } else {
            target.details = [
              ...(target.details ?? []),
              { label: '错误信息', text: summarizeErrorDetail(event.error) },
            ]
            target.detailLabel = '查看失败原因'
          }
        }
        if (event.details?.trim()) {
          target.details = [
            ...(target.details ?? []),
            { label: '命令输出或文件差异', text: event.details.trim() },
          ]
          target.detailLabel = '查看本轮输出'
        }
        break
      }
      case 'agent_command_approval_required': {
        if (pendingTool) {
          pendingTool.status = 'waiting'
          pendingTool.meta = '等待人工批准'
        } else {
          rows.push({
            id,
            icon: 'pause',
            action: '等待批准',
            summary: summarizeText(event.command),
            status: 'waiting',
          })
        }
        break
      }
      case 'agent_command_approval_decision': {
        if (!pendingTool) break
        pendingTool.status = event.approved ? 'running' : 'failed'
        pendingTool.meta = event.approved ? '已批准' : '已拒绝'
        break
      }
      // 计划与任务生命周期事件由输入框上方的任务卡片呈现，不进入轨迹列表
      case 'agent_todo_started':
      case 'agent_todo_completed':
      case 'agent_todo_failed':
        break
      default:
        break
    }
  })

  if (running) {
    // 已发起但还没等到结果的工具行必定是最后一行（其批准与观测都并入本行）；
    // 等待人工批准时保持等待态，不再补一行思考占位。
    const last = rows[rows.length - 1]
    if (last?.status === 'running') {
      if (!last.meta) last.meta = '执行中'
    } else if (last?.status !== 'waiting') {
      rows.push({
        id: 'trace-running',
        icon: 'think',
        action: '思考',
        summary: '正在继续分析…',
        status: 'running',
      })
    }
  }

  return rows
}
