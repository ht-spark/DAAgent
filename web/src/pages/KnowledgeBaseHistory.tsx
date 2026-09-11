import { useCallback, useEffect, useState } from 'react'

import {
  deleteKnowledgeBase,
  getKnowledgeBases,
  updateKnowledgeBase,
  type KnowledgeBaseSummary,
} from '../api'

export default function KnowledgeBaseHistory({
  onOpenKnowledgeBase,
}: {
  onOpenKnowledgeBase: (knowledgeBaseId: string) => void
}) {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState('')

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

  async function editBase(base: KnowledgeBaseSummary) {
    const name = window.prompt('知识库名称', base.name)?.trim()
    if (!name) return
    const description = window.prompt('知识库说明', base.description)?.trim()
    if (description === undefined) return
    setBusyId(base.id)
    setError('')
    try {
      const updated = await updateKnowledgeBase(base.id, name, description)
      setKnowledgeBases((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '修改知识库失败。')
    } finally {
      setBusyId(null)
    }
  }

  async function removeBase(base: KnowledgeBaseSummary) {
    const warning = base.document_count > 0
      ? `“${base.name}”包含 ${base.document_count} 份文件和 ${base.indexed_chunks} 个向量分块。删除后不可恢复，确定继续吗？`
      : `确定删除空知识库“${base.name}”吗？`
    if (!window.confirm(warning)) return
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
                    <button type="button" className="api-btn" onClick={() => onOpenKnowledgeBase(base.id)}>管理</button>
                    <button type="button" className="api-btn" disabled={busyId === base.id} onClick={() => void editBase(base)}>编辑</button>
                    <button type="button" className="api-btn danger" disabled={busyId === base.id} onClick={() => void removeBase(base)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
