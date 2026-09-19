// 与 FastAPI 后端交互的薄封装。开发期经 Vite 代理（/api -> :8000）。

import {
  ApiConfig,
  ApiSettingsSnapshot,
  DEFAULT_EXTERNAL_SERVICES,
  ExternalService,
  ExternalServiceConfig,
  loadExternalServiceConfigs,
  saveActiveId,
  saveConfigs,
  saveExternalServiceConfigs,
} from './apiConfigs'

/**
 * 探测后端是否在线（GET /healthz）。
 */
export async function checkBackendOnline(): Promise<boolean> {
  try {
    const res = await fetch('/healthz', { signal: AbortSignal.timeout(3000) })
    return res.ok
  } catch {
    return false
  }
}

/**
 * 从非 OK 响应中尽力提取后端返回的 detail（支持 JSON 或纯文本）。
 * 失败时回退到通用模板，便于本地调试时直接看到具体异常类型与消息。
 */
async function extractDetail(res: Response, fallback: string): Promise<string> {
  // 优先尝试 JSON：FastAPI 的 HTTPException 序列化为 {"detail": "..."}
  try {
    const data = await res.clone().json()
    if (data && typeof (data as any).detail === 'string') return (data as any).detail
    const s = JSON.stringify(data)
    if (s && s !== '{}') return s
  } catch {
    /* 非 JSON，继续尝试 text */
  }
  try {
    const t = await res.text()
    if (t) {
      // Vite 代理错误通常是 HTML，提取关键信息
      if (t.includes('proxy') || t.includes('ECONNREFUSED')) {
        return '后端服务未启动或已崩溃，请检查服务器是否正在运行（端口 8000）'
      }
      return t.slice(0, 500)
    }
  } catch {
    /* ignore */
  }
  return `${fallback} (${res.status})`
}

export async function createRun(form: FormData): Promise<{ run_id: string; status: string; output_dir: string }> {
  let res: Response
  try {
    res = await fetch('/api/runs', {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(30000), // 30 秒超时，避免无限等待
    })
  } catch (e) {
    // 网络层错误（如后端未启动 / CORS / 连接拒绝 / 超时）
    const msg = e instanceof Error ? e.message : String(e)
    if (msg.includes('aborted') || msg.includes('timeout') || msg.includes('Timeout')) {
      throw new Error('请求超时：服务器响应时间过长，请检查后端是否正常运行')
    }
    throw new Error('后端服务未启动或无法连接，请确认服务器正在运行（uvicorn server.main:app --port 8000）')
  }
  if (!res.ok) {
    throw new Error(await extractDetail(res, '提交失败'))
  }
  return res.json()
}

export async function getRun(runId: string): Promise<any> {
  const res = await fetch(`/api/runs/${runId}`)
  if (!res.ok) throw new Error('获取运行状态失败')
  return res.json()
}

export async function getPaper(runId: string): Promise<string> {
  const res = await fetch(`/api/runs/${runId}/paper`)
  if (!res.ok) throw new Error('获取报告失败')
  return res.text()
}

export async function getFigures(runId: string): Promise<string[]> {
  const res = await fetch(`/api/runs/${runId}/figures`)
  if (!res.ok) return []
  const d = await res.json()
  return d.figures || []
}

export async function listRuns(): Promise<any[]> {
  const res = await fetch(`/api/runs`)
  if (!res.ok) throw new Error('获取历史失败')
  return res.json()
}

export async function deleteRun(runId: string): Promise<void> {
  const res = await fetch(`/api/runs/${runId}`, { method: 'DELETE' })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || `删除失败 (${res.status})`)
  }
}

export async function cancelRun(runId: string): Promise<void> {
  const res = await fetch(`/api/runs/${runId}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || `中断失败 (${res.status})`)
  }
}

export async function renameRun(runId: string, name: string): Promise<void> {
  const fd = new FormData()
  fd.append('name', name)
  const res = await fetch(`/api/runs/${runId}/name`, { method: 'PATCH', body: fd })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || `重命名失败 (${res.status})`)
  }
}

/** 确认某小问的预算覆盖（弹窗提交）。use_defaults=true 表示沿用默认。 */
export async function confirmBudget(
  runId: string,
  body: { question_id?: string; use_defaults?: boolean; limits?: Record<string, number> },
): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/runs/${runId}/budget-confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || `预算确认失败 (${res.status})`)
  }
  return res.json()
}

/** G0 硬失败澄清：选择终止或上传补充材料继续建模。 */
export async function submitClarification(
  runId: string,
  action: 'terminate' | 'continue',
  dataFiles?: File[],
): Promise<{ ok: boolean }> {
  const fd = new FormData()
  fd.append('action', action)
  if (dataFiles) {
    dataFiles.forEach((f) => fd.append('data_files', f))
  }
  const res = await fetch(`/api/runs/${runId}/clarification`, {
    method: 'POST',
    body: fd,
  })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error((e as any).detail || `澄清提交失败 (${res.status})`)
  }
  return res.json()
}

export type KnowledgeDocument = {
  id: string
  name: string
  size_bytes: number
  uploaded_at: string
  upload_success: boolean
  is_markdown: boolean
  is_conversion: boolean
  embedding_status: 'pending' | 'embedded'
  indexed_chunks: number
  knowledge_base_id: string
}

export type KnowledgeBaseSummary = {
  id: string
  name: string
  description: string
  created_at: string
  is_default: boolean
  document_count: number
  indexed_chunks: number
}

export type RunMonitor = {
  active: boolean
  time_total: number | null
  token_total: number | null
}

/** 读取任务的实时资源统计；由页面按 30 秒频率刷新。 */
export async function getRunMonitor(runId: string): Promise<RunMonitor> {
  const res = await fetch(`/api/runs/${runId}/monitor`)
  if (!res.ok) throw new Error('获取实时监控失败')
  return res.json()
}

/** 读取当前任务写入中的 run.log；日志尚未生成时返回空内容。 */
export async function getRunLog(runId: string): Promise<string> {
  const res = await fetch(`/api/runs/${runId}/files/run.log`)
  if (!res.ok) return ''
  return res.text()
}

export type KnowledgeStatus = {
  retrieval_ready: boolean
  documents: KnowledgeDocument[]
}

export async function getKnowledgeBases(): Promise<KnowledgeBaseSummary[]> {
  const res = await fetch('/api/knowledge/bases')
  if (!res.ok) throw new Error(await extractDetail(res, '获取知识库列表失败'))
  return res.json()
}

export async function createKnowledgeBase(
  name: string,
  description = '',
): Promise<KnowledgeBaseSummary> {
  const res = await fetch('/api/knowledge/bases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '创建知识库失败'))
  return res.json()
}

export async function updateKnowledgeBase(
  knowledgeBaseId: string,
  name: string,
  description = '',
): Promise<KnowledgeBaseSummary> {
  const res = await fetch(`/api/knowledge/bases/${encodeURIComponent(knowledgeBaseId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '修改知识库失败'))
  return res.json()
}

export async function deleteKnowledgeBase(
  knowledgeBaseId: string,
): Promise<{ deleted_id: string; deleted_document_ids: string[] }> {
  const res = await fetch(`/api/knowledge/bases/${encodeURIComponent(knowledgeBaseId)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(await extractDetail(res, '删除知识库失败'))
  return res.json()
}

export async function getKnowledgeStatus(knowledgeBaseId?: string): Promise<KnowledgeStatus> {
  const path = knowledgeBaseId
    ? `/api/knowledge/status?${new URLSearchParams({ knowledge_base_id: knowledgeBaseId })}`
    : '/api/knowledge/status'
  const res = await fetch(path)
  if (!res.ok) throw new Error(await extractDetail(res, '获取知识库状态失败'))
  return res.json()
}

export async function uploadKnowledgeDocument(
  file: File,
  knowledgeBaseId = 'default',
): Promise<KnowledgeDocument[]> {
  const form = new FormData()
  form.append('document', file)
  form.append('knowledge_base_id', knowledgeBaseId)
  form.append('mineru_config', JSON.stringify(loadExternalServiceConfigs().mineru))
  const res = await fetch('/api/knowledge/documents', { method: 'POST', body: form })
  if (!res.ok) throw new Error(await extractDetail(res, '文档上传失败'))
  return res.json()
}

export async function deleteKnowledgeDocuments(documentIds: string[]): Promise<{ deleted_ids: string[] }> {
  const res = await fetch('/api/knowledge/documents', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document_ids: documentIds }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '删除知识库文件失败'))
  return res.json()
}

export async function convertKnowledgeDocuments(documentIds: string[]): Promise<{ converted_ids: string[]; failed: string[] }> {
  const res = await fetch('/api/knowledge/documents/convert', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document_ids: documentIds }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '文档转换失败'))
  return res.json()
}

export type KnowledgeChunkEmbedResult = {
  collection_name: string
  documents_processed: number
  chunks_indexed: number
  vector_size: number
  document_chunks: Record<string, number>
}

export type KnowledgeChunkEmbedProgress = {
  stage: 'idle' | 'chunking' | 'embedding' | 'done' | 'failed'
  error: string | null
}

export async function chunkAndEmbedKnowledge(windowSize: number): Promise<KnowledgeChunkEmbedResult> {
  const res = await fetch('/api/knowledge/chunk-embed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ window_size: windowSize }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '分块与嵌入失败'))
  return res.json()
}

export async function getKnowledgeChunkEmbedProgress(): Promise<KnowledgeChunkEmbedProgress> {
  const res = await fetch('/api/knowledge/chunk-embed/progress')
  if (!res.ok) throw new Error(await extractDetail(res, '获取分块与嵌入进度失败'))
  return res.json()
}

export type ProjectSummary = {
  id: string
  title: string
  root_path: string
  created_at: string
  updated_at: string
}

export async function fetchProjects(): Promise<ProjectSummary[]> {
  const res = await fetch('/api/projects')
  if (!res.ok) throw new Error(await extractDetail(res, '获取项目列表失败'))
  return res.json()
}

export async function createProject(rootPath: string, title?: string): Promise<ProjectSummary> {
  const res = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ root_path: rootPath, title: title || null }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '添加项目失败'))
  return res.json()
}

export async function deleteProject(projectId: string): Promise<void> {
  const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await extractDetail(res, '移除项目失败'))
}

export type ProjectFileNode = {
  name: string
  path: string
  type: 'file' | 'dir'
  size_bytes?: number
  children?: ProjectFileNode[]
}

export type ProjectFileTree = {
  project_id: string
  title: string
  root_path: string
  nodes: ProjectFileNode[]
}

export async function fetchProjectFileTree(projectId: string): Promise<ProjectFileTree> {
  const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/files/tree`)
  if (!res.ok) throw new Error(await extractDetail(res, '获取项目文件树失败'))
  return res.json()
}

export async function openProjectFolder(projectId: string): Promise<void> {
  const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/open-folder`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error(await extractDetail(res, '无法打开项目文件夹'))
}

/** 打开系统文件夹选择框；用户取消时返回 null。 */
export async function selectSystemFolder(): Promise<string | null> {
  const res = await fetch('/api/system/select-folder', { method: 'POST' })
  if (!res.ok) throw new Error(await extractDetail(res, '无法打开系统文件夹选择框'))
  const data = (await res.json()) as { path: string | null }
  return data.path ?? null
}
export type BrainstormDiscussionSummary = {
  id: string
  title: string
  project_id?: string | null
  updated_at: string
}

export type BrainstormDiscussion = BrainstormDiscussionSummary & {
  messages: Array<{
    role: 'assistant' | 'user'
    content: string
    sources?: KnowledgeSourceCitation[]
    metadata?: {
      knowledge_base_id?: string
      knowledge_base_ids?: string[]
      mode?: 'inspiration' | 'agent'
      agent_run_id?: string
      agent_trace?: AgentTraceEvent[]
      agent_artifacts?: AgentArtifact[]
      context_usage?: ContextUsage
      sources?: KnowledgeSourceCitation[]
    }
  }>
}

export type AgentArtifact = {
  discussion_id?: string
  run_id: string
  path: string
  name: string
  size_bytes: number
}

export async function openAgentArtifactFolder(discussionId: string): Promise<void> {
  const res = await fetch(
    `/api/agent/discussions/${encodeURIComponent(discussionId)}/open-folder`,
    { method: 'POST' },
  )
  if (!res.ok) throw new Error(await extractDetail(res, '无法打开产物文件夹'))
}

export type KnowledgeSourceCitation = {
  source_file: string
  document_id?: string
  knowledge_base_id?: string
  excerpt?: string
}

export type AgentTraceEvent = {
  type: 'agent_started'
  run_id: string
  files: string[]
  knowledge_base_ids?: string[]
} | {
  type: 'agent_plan'
  objective: string
  todos: Array<{
    id: string
    objective: string
    depends_on: string[]
    expected_outputs: string[]
  }>
} | {
  type: 'agent_todo_started'
  todo_id: string
  objective: string
} | {
  type: 'agent_todo_completed'
  todo_id: string
  result_summary: string
} | {
  type: 'agent_todo_failed'
  todo_id: string
  error: string
} | {
  type: 'agent_action'
  thought: string
  tool_name: string
  code?: string
} | {
  type: 'agent_reasoning'
  summary: string
} | {
  type: 'agent_observation'
  tool_name: string
  success: boolean
  duration_ms: number
  summary?: Record<string, number> | null
  error?: string | null
  details?: string
  /** search_knowledge_base 命中的来源标注 */
  sources?: KnowledgeSourceCitation[]
} | {
  type: 'agent_command_approval_required'
  approval_id: string
  command: string
  python_environment?: string | null
  workspace_root: string
} | {
  type: 'agent_command_approval_decision'
  approval_id: string
  approved: boolean
}

export type ContextUsage = {
  estimated_tokens: number
  threshold_tokens: number
  remaining_tokens: number
}

export async function decideAgentCommand(approvalId: string, approved: boolean): Promise<void> {
  const res = await fetch(`/api/agent/commands/${encodeURIComponent(approvalId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '提交命令审批失败'))
}

const AGENT_REQUEST_TIMEOUT_MS = 3 * 60 * 60 * 1000 + 60 * 1000

export type AgentChatHandlers = {
  onStarted: (runId: string, files: string[], discussionId?: string, message?: string) => void
  onTrace: (event: AgentTraceEvent) => void
  onApprovalRequired: (event: Extract<AgentTraceEvent, { type: 'agent_command_approval_required' }>) => Promise<void>
  onToken: (content: string) => void
  onDone: (discussionId: string, runId: string, artifacts: AgentArtifact[], sources?: KnowledgeSourceCitation[], contextUsage?: ContextUsage) => void
  onCancelled?: () => void
}

export async function fetchActiveAgentChatRun(discussionId: string): Promise<{ run_id: string; message: string } | null> {
  const res = await fetch(`/api/agent/discussions/${encodeURIComponent(discussionId)}/active-run`)
  if (!res.ok) throw new Error(await extractDetail(res, '获取运行任务失败'))
  return res.json()
}

export async function cancelAgentChatRun(runId: string): Promise<void> {
  const res = await fetch(`/api/agent/chat/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(await extractDetail(res, '终止任务失败'))
}

export async function reconnectAgentChatRun(runId: string, handlers: AgentChatHandlers, signal: AbortSignal): Promise<void> {
  const res = await fetch(`/api/agent/chat/runs/${encodeURIComponent(runId)}/stream`, { signal })
  if (!res.ok) throw new Error(await extractDetail(res, '重连任务失败'))
  await consumeAgentChatStream(res, handlers)
}

export async function streamDataAgentMessage(
  message: string,
  discussionId: string | null,
  title: string,
  files: File[],
  handlers: AgentChatHandlers,
  configId?: string | null,
  knowledgeBaseIds?: string[],
  projectId?: string | null,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData()
  form.append('message', message)
  form.append('title', title)
  if (discussionId) form.append('discussion_id', discussionId)
  if (configId) form.append('config_id', configId)
  knowledgeBaseIds?.forEach((id) => form.append('knowledge_base_ids', id))
  if (projectId) form.append('project_id', projectId)
  files.forEach((file) => form.append('files', file))

  let res: Response
  try {
    res = await fetch('/api/agent/chat/stream', {
      method: 'POST',
      body: form,
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(AGENT_REQUEST_TIMEOUT_MS)]) : AbortSignal.timeout(AGENT_REQUEST_TIMEOUT_MS),
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new Error('Agent 请求超过 3 小时，请检查模型服务或缩小任务规模。')
    }
    throw new Error('Agent 无法连接，请确认后端服务正在运行。')
  }
  if (!res.ok) throw new Error(await extractDetail(res, 'Agent 启动失败'))
  await consumeAgentChatStream(res, handlers)
}

async function consumeAgentChatStream(res: Response, handlers: AgentChatHandlers): Promise<void> {
  if (!res.body) throw new Error('Agent 未返回流式内容。')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line) continue
      const event = JSON.parse(line) as {
        type: string
        content?: string
        detail?: string
        run_id?: string
        discussion_id?: string
        message?: string
        files?: string[]
        artifacts?: AgentArtifact[]
        sources?: KnowledgeSourceCitation[]
        context_usage?: ContextUsage
      }
      if (event.type === 'agent_started' && event.run_id) {
        handlers.onStarted(event.run_id, event.files ?? [], event.discussion_id, event.message)
        handlers.onTrace(event as AgentTraceEvent)
      }
      if (event.type === 'agent_plan' || event.type === 'agent_todo_started'
        || event.type === 'agent_todo_completed' || event.type === 'agent_todo_failed' || event.type === 'agent_action'
        || event.type === 'agent_reasoning' || event.type === 'agent_observation'
        || event.type === 'agent_command_approval_required' || event.type === 'agent_command_approval_decision') {
        handlers.onTrace(event as AgentTraceEvent)
      }
      if (event.type === 'agent_command_approval_required') {
        await handlers.onApprovalRequired(event as Extract<AgentTraceEvent, { type: 'agent_command_approval_required' }>)
      }
      if (event.type === 'token' && event.content) handlers.onToken(event.content)
      if (event.type === 'error') throw new Error(event.detail ?? '数据 Agent 运行失败。')
      if (event.type === 'cancelled') handlers.onCancelled?.()
      if (event.type === 'done' && event.discussion_id && event.run_id) {
        handlers.onDone(event.discussion_id, event.run_id, event.artifacts ?? [], event.sources, event.context_usage)
      }
    }
    if (done) break
  }
}

export async function fetchBrainstormDiscussions(projectId?: string | null): Promise<BrainstormDiscussionSummary[]> {
  const query = projectId ? `?${new URLSearchParams({ project_id: projectId })}` : ''
  const res = await fetch(`/api/knowledge/discussions${query}`)
  if (!res.ok) throw new Error(await extractDetail(res, '获取讨论历史失败'))
  return res.json()
}

export async function fetchBrainstormDiscussion(discussionId: string): Promise<BrainstormDiscussion> {
  const res = await fetch(`/api/knowledge/discussions/${discussionId}`)
  if (!res.ok) throw new Error(await extractDetail(res, '获取讨论记录失败'))
  return res.json()
}

export async function deleteBrainstormDiscussion(discussionId: string): Promise<void> {
  const res = await fetch(`/api/knowledge/discussions/${discussionId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await extractDetail(res, '删除讨论失败'))
}

export async function renameBrainstormDiscussion(discussionId: string, title: string): Promise<void> {
  const res = await fetch(`/api/knowledge/discussions/${discussionId}/title`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await extractDetail(res, '修改讨论名称失败'))
}

/**
 * 从后端本地文件读取 API 配置快照。
 * 后端不可用、或从未保存过（saved=false）时返回 null。
 */
export async function fetchApiSettings(): Promise<(ApiSettingsSnapshot & { saved: boolean }) | null> {
  try {
    const res = await fetch('/api/settings/api', { signal: AbortSignal.timeout(3000) })
    if (!res.ok) return null
    const d = await res.json()
    if (!Array.isArray(d.configs)) return null
    const es = (d.external_services ?? {}) as Record<string, any>
    const merge = (name: ExternalService): ExternalServiceConfig => ({
      apiKey: String(es[name]?.apiKey ?? DEFAULT_EXTERNAL_SERVICES[name].apiKey),
      baseUrl: String(es[name]?.baseUrl ?? DEFAULT_EXTERNAL_SERVICES[name].baseUrl),
    })
    return {
      saved: d.saved === true,
      configs: d.configs as ApiConfig[],
      activeId: typeof d.active_id === 'string' ? d.active_id : null,
      externalServices: { tavily: merge('tavily'), mineru: merge('mineru') },
    }
  } catch {
    return null
  }
}

/**
 * 把后端权威配置同步进浏览器缓存。
 * 仅当后端确实保存过（saved=true）时覆盖，避免首次迁移清掉本地已有配置。
 */
export async function syncApiSettingsCache(): Promise<boolean> {
  const snap = await fetchApiSettings()
  if (!snap || !snap.saved) return false
  saveConfigs(snap.configs)
  saveActiveId(snap.activeId)
  saveExternalServiceConfigs(snap.externalServices)
  return true
}

/** 将配置快照写入后端本地文件；失败时静默降级为仅浏览器缓存。 */
export async function persistApiSettings(snapshot: ApiSettingsSnapshot): Promise<void> {
  try {
    await fetch('/api/settings/api', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        configs: snapshot.configs,
        active_id: snapshot.activeId,
        external_services: snapshot.externalServices,
      }),
      signal: AbortSignal.timeout(5000),
    })
  } catch {
    /* 后端离线时保留 localStorage 缓存即可 */
  }
}
