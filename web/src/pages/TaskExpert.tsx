import { useEffect, useState } from 'react'

import { listExperts, type ExpertInfo } from '../api'

type Props = {
  onSelect: (expertId: string) => void
}

/** 从后端专家注册表加载可启动的任务专家。 */
export default function TaskExpert({ onSelect }: Props) {
  const [experts, setExperts] = useState<ExpertInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void listExperts()
      .then((items) => {
        if (active) setExperts(items)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : '获取专家列表失败')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <div className="page modeling-tasks-page task-expert-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">任务专家</h1>
          <p className="page-sub">选择一类专家能力，快速启动对应分析任务。</p>
        </div>
      </div>

      {loading ? <p className="skills-empty">正在读取专家…</p> : null}
      {error ? <p className="skills-error" role="alert">{error}</p> : null}
      {!loading && !error && experts.length === 0 ? (
        <p className="skills-empty">暂无可用专家</p>
      ) : null}

      <div className="expert-card-grid">
        {experts.map((expert) => (
          <button
            key={expert.expert_id}
            type="button"
            className="expert-card"
            onClick={() => onSelect(expert.expert_id)}
          >
            <div className="expert-card-head">
              <span className="expert-card-avatar" aria-hidden="true">
                {expert.glyph}
              </span>
              <div className="expert-card-identity">
                <strong className="expert-card-title">{expert.name}</strong>
                <span className="expert-card-alias">版本 {expert.version}</span>
              </div>
            </div>
            <p className="expert-card-desc">{expert.description}</p>
            <div className="expert-card-tags">
              {expert.tags.map((tag) => (
                <span key={tag} className="expert-card-tag">{tag}</span>
              ))}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}
