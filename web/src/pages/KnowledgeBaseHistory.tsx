import { useCallback, useEffect, useRef, useState } from 'react'

import {
  deleteKnowledgeBase,
  getKnowledgeBases,
  updateKnowledgeBase,
  type KnowledgeBaseSummary,
} from '../api'
import ConfirmDialog from '../components/ConfirmDialog'

export default function KnowledgeBaseHistory({
  onOpenKnowledgeBase,
}: {
  onOpenKnowledgeBase: (knowledgeBaseId: string) => void
}) {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [pendingDeleteBase, setPendingDeleteBase] = useState<KnowledgeBaseSummary | null>(null)
  const [editingBase, setEditingBase] = useState<KnowledgeBaseSummary | null>(null)
  const [editName, setEditName] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editError, setEditError] = useState('')
  const editNameRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setKnowledgeBases(await getKnowledgeBases())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取知识库统计。')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  function openEditDialog(base: KnowledgeBaseSummary) {
    setEditingBase(base)
    setEditName(base.name)
    setEditDescription(base.description)
    setEditError('')
  }

  function closeEditDialog() {
    if (editingBase && busyId === editingBase.id) return
    setEditingBase(null)
    setEditName('')
    setEditDescription('')
    setEditError('')
  }

  async function submitEditBase() {
    if (!editingBase || busyId === editingBase.id) return
    const name = editName.trim()
    if (!name) return
    setBusyId(editingBase.id)
    setError('')
    setEditError('')
    try {
      const updated = await updateKnowledgeBase(
        editingBase.id,
        name,
        editDescription.trim(),
      )
      setKnowledgeBases((current) => current.map((item) => item.id === updated.id ? updated : item))
      setEditingBase(null)
      setEditName('')
      setEditDescription('')
    } catch (reason) {
      setEditError(reason instanceof Error ? reason.message : '修改知识库失败。')
    } finally {
      setBusyId(null)
    }
  }

  useEffect(() => {
    if (!editingBase) return
    const previous = document.activeElement
    const timer = window.setTimeout(() => editNameRef.current?.focus(), 20)
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeEditDialog()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('keydown', onKeyDown)
      if (previous instanceof HTMLElement) previous.focus()
    }
  }, [editingBase, busyId])

  function requestRemoveBase(base: KnowledgeBaseSummary) {
    setPendingDeleteBase(base)
  }

  async function confirmRemoveBase() {
    const base = pendingDeleteBase
    if (!base) return
    setPendingDeleteBase(null)
    setBusyId(base.id)
    setError('')
    try {
      await deleteKnowledgeBase(base.id)
      setKnowledgeBases((current) => current.filter((item) => item.id !== base.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除知识库失败。')
    } finally {
      setBusyId(null)
    }
  }

  const totalDocuments = knowledgeBases.reduce((sum, base) => sum + base.document_count, 0)
  const totalChunks = knowledgeBases.reduce((sum, base) => sum + base.indexed_chunks, 0)

  return (
    <div className="page knowledge-base-page">
      <div className="page-head">
        <div>
          <h1 className="page-title">知识库统计</h1>
          <p className="page-sub">按知识库汇总文件与向量状态，并管理每个知识库。</p>
        </div>
      </div>

      {error && <div className="err-box">{error}</div>}

      <div className="ce-summary kb-catalog-summary">
        <div><span className="ce-summary-num">{knowledgeBases.length}</span><span className="ce-summary-label">知识库</span></div>
        <div><span className="ce-summary-num">{totalDocuments}</span><span className="ce-summary-label">文件总数</span></div>
        <div><span className="ce-summary-num">{totalChunks}</span><span className="ce-summary-label">向量分块</span></div>
      </div>

      <div className="kb-table-section">
        <div className="kb-table-head">
          <h2>全部知识库</h2>
          <span className="kb-table-count">{loading ? '读取中…' : `共 ${knowledgeBases.length} 个`}</span>
        </div>

        <div className="kb-table-wrapper">
          <table className="kb-table kb-history-table">
            <thead>
              <tr>
                <th className="kb-history-detail">名称</th>
                <th className="kb-history-detail">说明</th>
                <th className="kb-history-status">文件</th>
                <th className="kb-history-status">向量分块</th>
                <th className="kb-history-time">创建时间</th>
                <th className="kb-history-open">操作</th>
              </tr>
            </thead>
            <tbody>
              {!loading && knowledgeBases.length === 0 && (
                <tr>
                  <td colSpan={6} className="kb-empty-row">暂无知识库，可从“知识库更新”中创建</td>
                </tr>
              )}
              {knowledgeBases.map((base) => (
                <tr key={base.id}>
                  <td className="kb-history-detail"><strong>{base.name}</strong></td>
                  <td className="kb-history-detail" title={base.description}>{base.description || '—'}</td>
                  <td className="kb-history-status">{base.document_count}</td>
                  <td className="kb-history-status">{base.indexed_chunks}</td>
                  <td className="kb-history-time">
                    {base.created_at ? new Date(base.created_at).toLocaleDateString('zh-CN') : '旧数据'}
                  </td>
                  <td className="kb-history-open kb-base-actions">
                    <button type="button" className="api-btn" onClick={() => onOpenKnowledgeBase(base.id)}>查看文件</button>
                    <button type="button" className="api-btn" disabled={busyId === base.id} onClick={() => openEditDialog(base)}>编辑</button>
                    <button type="button" className="api-btn danger" disabled={busyId === base.id} onClick={() => requestRemoveBase(base)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {editingBase && (
        <div className="api-modal-mask" onClick={closeEditDialog} role="presentation">
          <div
            className="api-modal kb-create-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="kb-edit-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="api-modal-head">
              <div>
                <h2 id="kb-edit-title">编辑知识库</h2>
                <p className="api-modal-lead">修改知识库名称，并补充资料范围和适用场景。</p>
              </div>
              <button
                type="button"
                className="api-modal-close"
                onClick={closeEditDialog}
                aria-label="关闭"
                disabled={busyId === editingBase.id}
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
                void submitEditBase()
              }}
            >
              {editError && <div className="err-box">{editError}</div>}
              <div className="field">
                <label htmlFor="kb-edit-name">知识库名称</label>
                <input
                  ref={editNameRef}
                  id="kb-edit-name"
                  type="text"
                  value={editName}
                  onChange={(event) => setEditName(event.target.value)}
                  placeholder="例如：产品说明书 / 行业研报"
                  maxLength={64}
                  disabled={busyId === editingBase.id}
                  autoComplete="off"
                />
                <p className="api-field-hint">名称用于识别和选择知识库。</p>
              </div>
              <div className="field">
                <label htmlFor="kb-edit-description">知识库描述（选填）</label>
                <textarea
                  id="kb-edit-description"
                  value={editDescription}
                  onChange={(event) => setEditDescription(event.target.value)}
                  placeholder="例如：包含产品功能、使用说明和常见问题，适用于产品咨询与售后答疑。"
                  maxLength={300}
                  rows={4}
                  disabled={busyId === editingBase.id}
                />
                <p className="api-field-hint">简要说明资料范围和适用场景，便于后续选择合适的知识库。</p>
              </div>
            </form>

            <footer className="api-modal-foot">
              <button
                type="button"
                className="api-cancel"
                onClick={closeEditDialog}
                disabled={busyId === editingBase.id}
              >
                取消
              </button>
              <button
                type="button"
                className="api-save"
                onClick={() => void submitEditBase()}
                disabled={busyId === editingBase.id || !editName.trim()}
              >
                {busyId === editingBase.id ? '保存中…' : '保存'}
              </button>
            </footer>
          </div>
        </div>
      )}
      <ConfirmDialog
        open={Boolean(pendingDeleteBase)}
        title="删除知识库"
        description={
          pendingDeleteBase && pendingDeleteBase.document_count > 0
            ? `「${pendingDeleteBase.name}」包含 ${pendingDeleteBase.document_count} 份文件和 ${pendingDeleteBase.indexed_chunks} 个向量分块，将同时删除磁盘上的原始文件与 Markdown，删除后不可恢复。`
            : `确定删除空知识库「${pendingDeleteBase?.name ?? ''}」吗？`
        }
        confirmLabel="删除知识库"
        tone="danger"
        onConfirm={() => void confirmRemoveBase()}
        onCancel={() => setPendingDeleteBase(null)}
      />
    </div>
  )
}
