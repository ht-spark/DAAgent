import { useCallback, useEffect, useRef, useState } from 'react'

import {
  convertKnowledgeDocuments,
  createKnowledgeBase,
  deleteKnowledgeDocuments,
  getKnowledgeBases,
  type KnowledgeBaseSummary,
  type KnowledgeDocument,
  uploadKnowledgeDocument,
} from '../api'
import { formatKnowledgeDocumentId } from '../knowledgeDocumentId'
import ConfirmDialog from '../components/ConfirmDialog'
import { pushModalLayer } from '../modalLayer'

const PAGE_SIZE = 10

export default function KnowledgeBase({
  onNext,
  initialKnowledgeBaseId,
  onKnowledgeBaseIdChange,
}: {
  onNext: () => void
  initialKnowledgeBaseId?: string
  onKnowledgeBaseIdChange?: (knowledgeBaseId: string) => void
}) {
  // 本页只承载「一次上传任务」：打开时列表为空，不拉取历史文件
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  const [basesLoading, setBasesLoading] = useState(true)
  const [knowledgeBaseId, setKnowledgeBaseId] = useState(initialKnowledgeBaseId ?? '')
  const [currentPage, setCurrentPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<{ total: number; done: number; currentName: string } | null>(null)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [converting, setConverting] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [pendingDeleteDocs, setPendingDeleteDocs] = useState(false)
  // 新建知识库：用主题弹窗代替原生 prompt
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createDescription, setCreateDescription] = useState('')
  const [creating, setCreating] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const createDialogRef = useRef<HTMLDivElement>(null)
  const creatingRef = useRef(creating)
  creatingRef.current = creating
  const uploading = uploadProgress !== null
  useEffect(() => {
    setBasesLoading(true)
    void getKnowledgeBases()
      .then((bases) => {
        setKnowledgeBases(bases)
        // setState 的更新函数必须是纯函数，外部回调不能写在里面，
        // 否则 React 会在渲染期触发父组件更新并报 warning。
        setKnowledgeBaseId((current) => {
          const requested = initialKnowledgeBaseId && bases.some((base) => base.id === initialKnowledgeBaseId)
            ? initialKnowledgeBaseId
            : current
          return bases.some((base) => base.id === requested) ? requested : bases[0]?.id ?? ''
        })
      })
      .catch((reason: unknown) => setError(
        reason instanceof Error ? reason.message : '无法读取知识库列表。',
      ))
      .finally(() => setBasesLoading(false))
  }, [initialKnowledgeBaseId, onKnowledgeBaseIdChange])

  // 当前知识库变化时同步给上层，供「资料统计 / 分块嵌入」沿用同一个库
  useEffect(() => {
    if (knowledgeBaseId) onKnowledgeBaseIdChange?.(knowledgeBaseId)
  }, [knowledgeBaseId, onKnowledgeBaseIdChange])

  // 切换知识库时清空本次任务列表，不加载该库历史文件
  useEffect(() => {
    setDocuments([])
    setSelectedIds(new Set())
    setCurrentPage(1)
    setLoading(false)
  }, [knowledgeBaseId])

  const uploadFiles = useCallback(async (files: FileList | null | undefined) => {
    if (!files || files.length === 0) return
    if (!knowledgeBaseId) {
      setError('请先创建或选择知识库，再添加资料。')
      return
    }
    const fileList = Array.from(files)
    setUploadProgress({ total: fileList.length, done: 0, currentName: fileList[0].name })
    setError('')
    const failedNames: string[] = []
    for (let i = 0; i < fileList.length; i++) {
      const file = fileList[i]
      setUploadProgress((prev) => (prev ? { ...prev, currentName: file.name } : prev))
      try {
        const prepared = await uploadKnowledgeDocument(file, knowledgeBaseId)
        setDocuments((current) => [
          ...current.filter((item) => !prepared.some((document) => document.name === item.name)),
          ...prepared.map((doc) => ({
            ...doc,
            id: doc.id || crypto.randomUUID(),
            upload_success: doc.upload_success ?? true,
            is_markdown: doc.is_markdown ?? false,
            is_conversion: doc.is_conversion ?? false,
          })),
        ])
      } catch (reason) {
        failedNames.push(file.name)
      } finally {
        setUploadProgress((prev) => (prev ? { ...prev, done: i + 1 } : prev))
      }
    }
    if (failedNames.length > 0) {
      setError(`${failedNames.length} 个文件上传失败：${failedNames.join('、')}`)
    }
    setCurrentPage(1)
    setUploadProgress(null)
    if (fileInput.current) fileInput.current.value = ''
  }, [knowledgeBaseId])

  function openCreateDialog() {
    setCreateName('')
    setCreateDescription('')
    setCreateOpen(true)
  }

  function closeCreateDialog() {
    if (creating) return
    setCreateOpen(false)
    setCreateName('')
    setCreateDescription('')
  }

  async function submitCreateKnowledgeBase() {
    const name = createName.trim()
    if (!name || creating) return
    setCreating(true)
    setError('')
    try {
      const created = await createKnowledgeBase(name, createDescription.trim())
      setKnowledgeBases((current) => [...current, created])
      setKnowledgeBaseId(created.id)
      onKnowledgeBaseIdChange?.(created.id)
      setDocuments([])
      setCreateOpen(false)
      setCreateName('')
      setCreateDescription('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建知识库失败。')
    } finally {
      setCreating(false)
    }
  }

  useEffect(() => {
    if (!createOpen) return
    return pushModalLayer(() => {
      if (!creatingRef.current) setCreateOpen(false)
    }, createDialogRef.current)
  }, [createOpen])

  function handleDrop(event: React.DragEvent) {
    event.preventDefault()
    setDragOver(false)
    uploadFiles(event.dataTransfer.files)
  }

  function toggleSelect(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    if (selectedIds.size === documents.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(documents.map((doc) => doc.id)))
    }
  }

  async function convertFormat() {
    if (selectedIds.size === 0) return
    const documentIds = [...selectedIds]
    setConverting(true)
    setError('')
    try {
      const { converted_ids: convertedIds, failed } = await convertKnowledgeDocuments(documentIds)
      const convertedSet = new Set(convertedIds)
      setDocuments((current) =>
        current.map((doc) =>
          convertedSet.has(doc.id) ? { ...doc, is_conversion: true } : doc,
        ),
      )
      if (failed.length > 0) {
        setError(`${failed.length} 个文件转换失败：${failed.join('、')}`)
      } else {
        setSelectedIds(new Set())
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '格式转换失败。')
    } finally {
      setConverting(false)
    }
  }

  function requestDeleteSelected() {
    if (selectedIds.size === 0 || deleting) return
    setPendingDeleteDocs(true)
  }

  async function confirmDeleteSelected() {
    const documentIds = [...selectedIds]
    if (documentIds.length === 0 || deleting) {
      setPendingDeleteDocs(false)
      return
    }
    setPendingDeleteDocs(false)

    setDeleting(true)
    setError('')
    try {
      const { deleted_ids: deletedIds } = await deleteKnowledgeDocuments(documentIds)
      const deletedSet = new Set(deletedIds)
      setDocuments((current) => current.filter((document) => !deletedSet.has(document.id)))
      setSelectedIds(new Set())
      setCurrentPage(1)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除知识库文件失败。')
    } finally {
      setDeleting(false)
    }
  }

  const successCount = documents.filter((doc) => doc.upload_success).length
  const markdownCount = documents.filter((doc) => doc.is_markdown).length
  const conversionCount = documents.filter((doc) => doc.is_conversion && !doc.is_markdown).length
  const totalPages = Math.max(1, Math.ceil(documents.length / PAGE_SIZE))
  const safePage = Math.min(currentPage, totalPages)
  const paginatedDocuments = documents.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  useEffect(() => {
    setCurrentPage((page) => Math.min(page, totalPages))
  }, [totalPages])

  return (
    <div className="page knowledge-base-page">
      <div className="page-head">
        <div>
          <h1 className="page-title">知识库维护</h1>
          <p className="page-sub">添加参考资料，转换后用于对话检索。</p>
        </div>
      </div>

      {error && <div className="err-box">{error}</div>}

      <div className="kb-layout">
        {basesLoading ? (
          <div className="kb-loading-panel" role="status" aria-live="polite">正在读取知识库…</div>
        ) : !knowledgeBaseId ? (
          // 空状态：左侧说明与主操作，右侧入库流程，底部格式与限制说明。
          // 不用单张窄卡片——页面宽度足够，把"我是谁 / 下一步做什么 / 边界在哪"一次讲清。
          <section className="kb-intro" aria-labelledby="kb-intro-title">
            <div className="kb-intro-main">
              <p className="kb-intro-eyebrow">知识库 · 尚未创建</p>
              <h2 id="kb-intro-title" className="kb-intro-title">先建一个知识库，再把资料放进去</h2>
              <p className="kb-intro-lead">
                知识库按主题归集文档。资料上传后会转成 Markdown 并建立索引，
                之后在分析任务里可以直接检索、引用到具体段落。
              </p>
              <div className="kb-intro-actions">
                <button type="button" className="kb-intro-cta" onClick={openCreateDialog}>
                  创建知识库
                </button>
              </div>
              <p className="kb-intro-note">本次任务的临时附件也可以直接在对话中添加，不必入库。</p>
            </div>

            <div className="kb-intro-flow">
              <p className="kb-intro-flow-label">入库流程</p>
              <ol className="kb-steps">
                <li className="kb-step">
                  <span className="kb-step-index" aria-hidden="true">01</span>
                  <span className="kb-step-body">
                    <strong>创建知识库</strong>
                    <span>按主题命名，一个库对应一类资料。</span>
                  </span>
                </li>
                <li className="kb-step">
                  <span className="kb-step-index" aria-hidden="true">02</span>
                  <span className="kb-step-body">
                    <strong>上传并转换</strong>
                    <span>PDF、Word、Excel、TXT、Markdown 统一转成可检索文本。</span>
                  </span>
                </li>
                <li className="kb-step">
                  <span className="kb-step-index" aria-hidden="true">03</span>
                  <span className="kb-step-body">
                    <strong>在任务中引用</strong>
                    <span>对话与数据分析时按需检索，结论可回溯到原文。</span>
                  </span>
                </li>
              </ol>
            </div>

            <div className="kb-intro-foot">
              <span>支持 PDF / Word / Excel / TXT / Markdown</span>
              <span>压缩包自动分批处理</span>
            </div>
          </section>
        ) : (
          <>
            {/* 选库是「上传到哪里」的输入条件，和上传区放在一起，不再占页头位置 */}
            <div className="kb-workbar">
              <label className="kb-workbar-field">
                <span>当前知识库</span>
                <select
                  value={knowledgeBaseId}
                  onChange={(event) => setKnowledgeBaseId(event.target.value)}
                  disabled={knowledgeBases.length === 0 || uploading || converting || deleting}
                >
                  {knowledgeBases.map((base) => (
                    <option key={base.id} value={base.id}>
                      {base.name} · {base.document_count} 份
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className="kb-workbar-new" onClick={openCreateDialog}>
                新建知识库
              </button>
            </div>

            <div
              className={`kb-upload-zone ${dragOver ? 'kb-drag-over' : ''} ${uploading ? 'kb-uploading' : ''}`}
              onClick={() => {
                if (!uploading) fileInput.current?.click()
              }}
              onDragOver={(event) => {
                event.preventDefault()
                if (!uploading) setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(event) => {
                if (uploading) { event.preventDefault(); return }
                handleDrop(event)
              }}
            >
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(event) => uploadFiles(event.target.files)}
              />
              {uploading ? (
                <div className="kb-upload-progress">
                  <div className="kb-upload-progress-top">
                    <span className="kb-upload-progress-count">
                      已上传 {uploadProgress?.done ?? 0} / {uploadProgress?.total ?? 0}
                    </span>
                    <span className="kb-upload-progress-name" title={uploadProgress?.currentName}>
                      {uploadProgress?.currentName}
                    </span>
                  </div>
                  <div className="kb-upload-progress-bar">
                    <div
                      className="kb-upload-progress-fill"
                      style={{
                        width: `${uploadProgress && uploadProgress.total > 0 ? (uploadProgress.done / uploadProgress.total) * 100 : 0}%`,
                      }}
                    />
                  </div>
                </div>
              ) : (
                <>
                  <span className="kb-upload-chip" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 16V4" />
                      <path d="m7 9 5-5 5 5" />
                      <path d="M4 16v2a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-2" />
                    </svg>
                  </span>
                  <span className="kb-upload-copy">
                    <span className="kb-upload-text">拖入文件，或从本地选择</span>
                    <span className="kb-upload-hint">PDF / Word / Excel / TXT / Markdown · 压缩包自动分批</span>
                  </span>
                  <button
                    type="button"
                    className="kb-select-files"
                    onClick={(event) => {
                      event.stopPropagation()
                      fileInput.current?.click()
                    }}
                  >
                    选择文件
                  </button>
                </>
              )}
            </div>
          </>
        )}

        {/* 下方：本次任务文件列表（不展示历史） */}
        {knowledgeBaseId && (
        <div className="kb-table-section">
          <div className="kb-table-head">
            <div className="kb-table-heading">
              <h2>本次上传文件</h2>
              <span className="kb-table-total">{loading ? '读取中…' : `共 ${documents.length} 份`}</span>
            </div>
            <ul className="kb-stat-strip" aria-label="本次上传统计">
              <li><span>成功</span><strong>{successCount}</strong></li>
              <li><span>Markdown</span><strong>{markdownCount}</strong></li>
              <li><span>已转换</span><strong>{conversionCount}</strong></li>
            </ul>
          </div>

          <div className="kb-table-wrapper">
            <table className="kb-table">
              <thead>
                <tr>
                  <th className="kb-col-check">
                    <input
                      type="checkbox"
                      checked={selectedIds.size === documents.length && documents.length > 0}
                      onChange={toggleSelectAll}
                      aria-label="全选"
                    />
                  </th>
                  <th className="kb-col-id">ID</th>
                  <th className="kb-col-name">文件名</th>
                  <th className="kb-col-time">上传时间</th>
                  <th className="kb-col-status">上传状态</th>
                  <th className="kb-col-unknown">Markdown 格式</th>
                  <th className="kb-col-status">是否转换成功</th>
                </tr>
              </thead>
              <tbody>
                {!loading && documents.length === 0 && (
                  <tr>
                    <td colSpan={7} className="kb-empty-row">
                      <div className="kb-table-empty">
                        <strong>还没有文件</strong>
                        <span>把文件拖到上方区域，或点「选择文件」开始上传。</span>
                      </div>
                    </td>
                  </tr>
                )}
                {paginatedDocuments.map((doc) => (
                  <tr
                    key={doc.id}
                    className={selectedIds.has(doc.id) ? 'kb-row-selected' : ''}
                    onClick={() => toggleSelect(doc.id)}
                  >
                    <td className="kb-col-check">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(doc.id)}
                        onChange={() => toggleSelect(doc.id)}
                        onClick={(event) => event.stopPropagation()}
                        aria-label={`选择 ${doc.name}`}
                      />
                    </td>
                    <td className="kb-col-id" title={doc.id}>{formatKnowledgeDocumentId(doc.id)}</td>
                    <td className="kb-col-name" title={doc.name}>{doc.name}</td>
                    <td className="kb-col-time">
                      {doc.uploaded_at
                        ? new Date(doc.uploaded_at).toLocaleString('zh-CN', {
                            month: '2-digit',
                            day: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit',
                          })
                        : '—'}
                    </td>
                    <td className="kb-col-status">
                      {doc.upload_success ? (
                        <span className="kb-tag kb-tag-success">成功</span>
                      ) : (
                        <span className="kb-tag kb-tag-fail">失败</span>
                      )}
                    </td>
                    <td className="kb-col-unknown">
                      {doc.is_markdown ? (
                        <span className="kb-tag kb-tag-success">是</span>
                      ) : (
                        <span className="kb-tag kb-tag-normal">否</span>
                      )}
                    </td>
                    <td className="kb-col-status">
                      {doc.is_markdown ? (
                        <span className="kb-tag kb-tag-normal">—</span>
                      ) : doc.is_conversion ? (
                        <span className="kb-tag kb-tag-success">成功</span>
                      ) : (
                        <span className="kb-tag kb-tag-normal">否</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 只有一页时不显示分页器，避免纯噪音 */}
          {totalPages > 1 && (
            <div className="kb-pagination">
              <button
                className="kb-page-btn"
                disabled={safePage <= 1}
                onClick={() => setCurrentPage((page) => Math.max(page - 1, 1))}
                aria-label="上一页"
              >
                上一页
              </button>
              <span className="kb-page-info">
                第 {safePage} / {totalPages} 页
              </span>
              <button
                className="kb-page-btn"
                disabled={safePage >= totalPages}
                onClick={() => setCurrentPage((page) => Math.min(page + 1, totalPages))}
                aria-label="下一页"
              >
                下一页
              </button>
            </div>
          )}

          <div className="kb-actions">
            <div className="kb-actions-utils">
              <button
                className="api-btn"
                disabled={selectedIds.size === 0 || converting}
                onClick={convertFormat}
              >
                {converting ? '转换中…' : '转换格式'}
              </button>
              <button
                className="api-btn danger"
                disabled={selectedIds.size === 0 || deleting}
                onClick={requestDeleteSelected}
              >
                {deleting ? '删除中…' : `删除所选 (${selectedIds.size})`}
              </button>
            </div>
            <button
              className="api-btn primary kb-actions-next"
              disabled={documents.length === 0}
              onClick={onNext}
            >
              下一步
            </button>
          </div>
        </div>
        )}
      </div>

      {createOpen && (
        <div className="api-modal-mask" onClick={closeCreateDialog} role="presentation">
          <div
            ref={createDialogRef}
            className="api-modal kb-create-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="kb-create-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="api-modal-head">
              <div>
                <h2 id="kb-create-title">新建知识库</h2>
                <p className="api-modal-lead">填写名称和内容描述，创建后可直接上传文档。</p>
              </div>
              <button
                type="button"
                className="api-modal-close"
                onClick={closeCreateDialog}
                aria-label="关闭"
                disabled={creating}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
                </svg>
              </button>
            </header>

            <form
              className="api-modal-body"
              onSubmit={(event) => {
                event.preventDefault()
                void submitCreateKnowledgeBase()
              }}
            >
              <div className="field">
                <label htmlFor="kb-create-name">知识库名称</label>
                <input
                  id="kb-create-name"
                  type="text"
                  value={createName}
                  onChange={(event) => setCreateName(event.target.value)}
                  placeholder="例如：产品说明书 / 行业研报"
                  maxLength={64}
                  disabled={creating}
                  autoComplete="off"
                />
                <p className="api-field-hint">名称用于识别和选择知识库。</p>
              </div>
              <div className="field">
                <label htmlFor="kb-create-description">知识库描述（选填）</label>
                <textarea
                  id="kb-create-description"
                  value={createDescription}
                  onChange={(event) => setCreateDescription(event.target.value)}
                  placeholder="例如：包含产品功能、使用说明和常见问题，适用于产品咨询与售后答疑。"
                  maxLength={300}
                  rows={4}
                  disabled={creating}
                />
                <p className="api-field-hint">简要说明资料范围和适用场景，便于后续选择合适的知识库。</p>
              </div>
            </form>

            <footer className="api-modal-foot">
              <button
                type="button"
                className="api-cancel"
                onClick={closeCreateDialog}
                disabled={creating}
              >
                取消
              </button>
              <button
                type="button"
                className="api-save"
                onClick={() => void submitCreateKnowledgeBase()}
                disabled={creating || !createName.trim()}
              >
                {creating ? '创建中…' : '创建'}
              </button>
            </footer>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={pendingDeleteDocs}
        title="删除知识库文件"
        description={`确定删除选中的 ${selectedIds.size} 份知识库文件吗？删除后不可恢复。`}
        confirmLabel={deleting ? '删除中…' : '删除文件'}
        tone="danger"
        onConfirm={() => void confirmDeleteSelected()}
        onCancel={() => setPendingDeleteDocs(false)}
      />
    </div>
  )
}
