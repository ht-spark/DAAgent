import { useEffect, useMemo, useState } from 'react'

import { getKnowledgeStatus, type KnowledgeDocument } from '../api'
import { loadKnowledgeHistory, type KnowledgeHistoryAction, type KnowledgeHistoryEvent } from '../knowledgeHistory'

const ACTION_LABELS: Record<KnowledgeHistoryAction, string> = {
  upload: '上传文件',
  delete: '删除文件',
  convert: '转化格式',
}

const STATUS_LABELS = {
  success: '成功',
  failure: '失败',
}

function canOpenUploadTask(event: KnowledgeHistoryEvent): boolean {
  return event.action === 'upload' && Array.isArray(event.documentIds) && event.documentIds.length > 0
}

function embeddingSummary(event: KnowledgeHistoryEvent, documents: Map<string, KnowledgeDocument>): string {
  if (!event.documentIds?.length) return '—'
  const batch = event.documentIds.map((id) => documents.get(id)).filter(Boolean) as KnowledgeDocument[]
  if (batch.length === 0) return '文件已删除'
  const indexed = batch.filter((document) => document.embedding_status === 'embedded')
  if (indexed.length === 0) return '待入库'
  const chunks = indexed.reduce((sum, document) => sum + document.indexed_chunks, 0)
  return indexed.length === batch.length ? `已入库 · ${chunks} 块` : `部分入库 · ${chunks} 块`
}

export default function KnowledgeBaseHistory({
  onOpenUploadTask,
}: {
  onOpenUploadTask?: (documentIds: string[]) => void
}) {
  const events = useMemo(() => loadKnowledgeHistory(), [])
  const [documents, setDocuments] = useState<Map<string, KnowledgeDocument>>(new Map())

  useEffect(() => {
    void getKnowledgeStatus().then((status) => {
      setDocuments(new Map(status.documents.map((document) => [document.id, document])))
    })
  }, [])

  return (
    <div className="page knowledge-base-page">
      <div className="page-head">
        <div>
          <h1 className="page-title">历史记录</h1>
          <p className="page-sub">记录历次知识库维护操作，并显示每批资料的向量入库状态。</p>
        </div>
      </div>

      <div className="kb-table-section">
        <div className="kb-table-head">
          <h2>操作记录</h2>
          <span className="kb-table-count">共 {events.length} 条</span>
        </div>

        <div className="kb-table-wrapper">
          <table className="kb-table kb-history-table">
            <thead>
              <tr>
                <th className="kb-history-time">时间</th>
                <th className="kb-history-action">操作</th>
                <th className="kb-history-detail">详情</th>
                <th className="kb-history-status">状态</th>
                <th className="kb-history-status">向量状态</th>
                <th className="kb-history-open">操作入口</th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 && (
                <tr>
                  <td colSpan={6} className="kb-empty-row">暂无知识库维护记录</td>
                </tr>
              )}
              {events.map((event) => (
                <tr key={event.id}>
                  <td className="kb-history-time">
                    {new Date(event.timestamp).toLocaleString('zh-CN', {
                      year: 'numeric',
                      month: '2-digit',
                      day: '2-digit',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </td>
                  <td className="kb-history-action">{ACTION_LABELS[event.action]}</td>
                  <td className="kb-history-detail" title={event.detail}>{event.detail}</td>
                  <td className="kb-history-status">
                    <span className={`kb-tag kb-tag-${event.status === 'failure' ? 'fail' : 'success'}`}>{STATUS_LABELS[event.status]}</span>
                  </td>
                  <td className="kb-history-status">{embeddingSummary(event, documents)}</td>
                  <td className="kb-history-open">
                    {canOpenUploadTask(event) && onOpenUploadTask && (
                      <button
                        type="button"
                        className="api-btn"
                        onClick={() => onOpenUploadTask(event.documentIds!)}
                      >
                        查看上传任务
                      </button>
                    )}
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
