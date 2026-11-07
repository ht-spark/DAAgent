import { useEffect, useRef, useState } from 'react'

import { deleteSkill, importSkill, listSkills, skillExportUrl, type SkillInfo, type SkillSource } from '../api'
import ConfirmDialog from '../components/ConfirmDialog'
import MoreActions from '../components/MoreActions'
import { pushModalLayer } from '../modalLayer'

/** 管理项目目录中已安装的文件化 Skill。 */
export default function Skills() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [activeTab, setActiveTab] = useState<SkillSource>('system')
  const [error, setError] = useState('')
  // 详情弹窗当前展示的技能，null 表示弹窗关闭
  const [detailSkill, setDetailSkill] = useState<SkillInfo | null>(null)
  // 等待用户二次确认删除的技能，null 表示确认框关闭
  const [pendingDelete, setPendingDelete] = useState<SkillInfo | null>(null)
  const [deleting, setDeleting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  async function refresh() {
    setLoading(true)
    setError('')
    try {
      setSkills(await listSkills())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '获取技能列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function upload(file: File) {
    setUploading(true)
    setError('')
    try {
      await importSkill(file)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导入技能失败')
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  /** 用户确认删除后调用后端卸载，成功则同步关闭详情与确认框。 */
  async function confirmDelete() {
    if (!pendingDelete || pendingDelete.source !== 'personal' || deleting) return
    setDeleting(true)
    setError('')
    try {
      await deleteSkill(pendingDelete.skill_id)
      setDetailSkill(null)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除技能失败')
    } finally {
      setPendingDelete(null)
      setDeleting(false)
    }
  }

  return (
    <div className="page modeling-tasks-page skills-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">技能</h1>
          <p className="page-sub">为分析任务添加可复用的能力，查看每项技能的用途与使用说明。</p>
        </div>
        {activeTab === 'personal' && (loading || skills.some((skill) => skill.source === 'personal')) && <button
          type="button"
          className="skills-upload-button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {uploading ? '正在上传…' : '上传个人技能'}
        </button>}
        <input
          ref={inputRef}
          hidden
          type="file"
          accept=".zip,application/zip"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void upload(file)
          }}
        />
      </div>

      <div className="skills-tabs" role="tablist" aria-label="技能来源">
        {(['system', 'personal'] as const).map((source) => (
          <button
            key={source}
            id={`skills-tab-${source}`}
            type="button"
            role="tab"
            className="skills-tab"
            aria-selected={activeTab === source}
            aria-controls={`skills-panel-${source}`}
            tabIndex={activeTab === source ? 0 : -1}
            onClick={() => setActiveTab(source)}
            onKeyDown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
              event.preventDefault()
              const next = event.key === 'Home' ? 'system' : event.key === 'End' ? 'personal' : source === 'system' ? 'personal' : 'system'
              setActiveTab(next)
              document.getElementById(`skills-tab-${next}`)?.focus()
            }}
          >
            {source === 'system' ? '系统' : '个人'}
            {!loading && <span className="skills-tab-count">{skills.filter((skill) => skill.source === source).length}</span>}
          </button>
        ))}
      </div>
      {error && <p className="skills-error" role="alert">{error}</p>}
      {(['system', 'personal'] as const).map((source) => (
        <section key={source} id={`skills-panel-${source}`} role="tabpanel" aria-labelledby={`skills-tab-${source}`} hidden={activeTab !== source} tabIndex={0}>
        <p className="skills-panel-description">{source === 'system' ? '平台内置的基础能力，由系统统一维护。' : '上传自己的技能，为数据分析任务扩展能力。支持包含 SKILL.md 的 ZIP 包。'}</p>
      {loading ? (
        <p className="skills-empty">正在读取已安装技能…</p>
      ) : skills.filter((skill) => skill.source === source).length === 0 ? (
        <div className="skills-empty">
          <strong>{source === 'system' ? '暂无系统技能' : '还没有个人技能'}</strong>
          <span>{source === 'system' ? '系统技能加载后会显示在这里。' : '上传后，即可在分析任务中选择使用。'}</span>
          {source === 'personal' && <button type="button" className="skills-upload-button" disabled={uploading} onClick={() => inputRef.current?.click()}>{uploading ? '正在上传…' : '上传个人技能'}</button>}
        </div>
      ) : (
        // 先展示能力说明，次要管理操作收进菜单。
        <ul className="skills-grid">
          {skills.filter((skill) => skill.source === source).map((skill) => (
            <li className="skill-card" key={skill.skill_id}>
              <span className="skill-card-name" title={skill.name}>
                {skill.name}
              </span>
              <p className="skill-card-description" title={skill.description}>{skill.description || '查看详情了解这项技能的用途与使用要求。'}</p>
              <div className="skill-card-meta" aria-label="技能资源">
                <span>{skill.references.length} 份参考资料</span>
                <span>{skill.assets.length} 个附件</span>
                <span>{skill.scripts.length} 个脚本</span>
              </div>
              <span className="skill-card-actions">
                <button
                  type="button"
                  className="skill-action"
                  onClick={() => setDetailSkill(skill)}
                >
                  查看详情
                </button>
                {source === 'system' ? <span className="skill-system-label">系统内置</span> : <MoreActions label={skill.name} actions={[{ label: '删除技能', danger: true, onSelect: () => setPendingDelete(skill) }]} />}
              </span>
            </li>
          ))}
        </ul>
      )}
        </section>
      ))}

      {detailSkill ? (
        <SkillDetailDialog
          skill={detailSkill}
          onClose={() => setDetailSkill(null)}
          onRequestDelete={detailSkill.source === 'personal' ? () => setPendingDelete(detailSkill) : undefined}
        />
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`删除技能「${pendingDelete?.name ?? ''}」`}
        description="删除后，后续任务将无法再使用这项个人技能。重新上传对应 ZIP 包即可恢复。"
        detail={
          pendingDelete
            ? `技能标识 ${pendingDelete.skill_id} · 版本 ${pendingDelete.bundle_hash.slice(0, 12)}`
            : undefined
        }
        confirmLabel={deleting ? '正在删除…' : '删除'}
        tone="danger"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}

/** 技能详情弹窗：展示完整声明内容，可按使用场景提供删除入口。 */
export function SkillDetailDialog({
  skill,
  onClose,
  onRequestDelete,
}: {
  skill: SkillInfo
  onClose: () => void
  onRequestDelete?: () => void
}) {
  // onClose 为内联箭头函数，用 ref 取最新值，避免每次渲染重新注册模态层
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  // 弹窗容器：把 Tab 焦点限制在弹窗内部
  const dialogRef = useRef<HTMLDivElement | null>(null)

  // 压入模态层：统一处理页面滚动锁、Esc 只作用于最上层弹窗、Tab 焦点不外漏
  useEffect(() => pushModalLayer(() => onCloseRef.current(), dialogRef.current), [])

  const hasResources = Boolean(skill.references.length || skill.assets.length || skill.scripts.length)

  return (
    <div className="confirm-mask" onClick={onClose} role="presentation">
      <div
        ref={dialogRef}
        className="confirm-dialog skill-detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-detail-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="confirm-head skill-detail-head">
          <div>
            <p className="confirm-kicker">技能详情</p>
            <h2 id="skill-detail-title">{skill.name}</h2>
            <code className="skill-detail-id">{skill.skill_id}</code>
          </div>
          <button
            type="button"
            className="skill-detail-close"
            onClick={onClose}
            aria-label="关闭技能详情"
          >
            ×
          </button>
        </header>
        <div className="confirm-body skill-detail-body">
          <p className="skill-detail-desc">{skill.description}</p>
          <div className="skill-counts">
            <span>资料 {skill.references.length}</span>
            <span>资产 {skill.assets.length}</span>
            <span>脚本 {skill.scripts.length}</span>
          </div>
          {hasResources ? (
            <div className="skill-resources">
              <SkillResourceList title="References" items={skill.references.map((item) => ({
                id: item.resource_id,
                description: item.description,
              }))} />
              <SkillResourceList title="Assets" items={skill.assets.map((item) => ({
                id: item.resource_id,
                description: item.description,
              }))} />
              <SkillResourceList title="Scripts" items={skill.scripts.map((item) => ({
                id: item.entrypoint_id,
                description: `${item.description}${item.requires_approval ? '（需审批）' : ''}`,
              }))} />
            </div>
          ) : (
            <p className="skill-detail-none">该技能没有声明额外资料、资产或脚本。</p>
          )}
          <small className="skill-hash">版本 {skill.bundle_hash.slice(0, 12)}</small>
        </div>
        <footer className="confirm-foot skill-detail-foot">
          <a
            className="skill-detail-export"
            href={skillExportUrl(skill.skill_id)}
            download={`${skill.skill_id}.zip`}
          >
            导出 ZIP
          </a>
          {onRequestDelete && skill.source === 'personal' ? (
            <button type="button" className="confirm-ok is-danger" onClick={onRequestDelete}>
              删除技能
            </button>
          ) : null}
        </footer>
      </div>
    </div>
  )
}

function SkillResourceList({
  title,
  items,
}: {
  title: string
  items: Array<{ id: string; description: string }>
}) {
  if (!items.length) return null
  return (
    <section>
      <h3>{title}</h3>
      {items.map((item) => (
        <p key={item.id}>
          <code>{item.id}</code>
          <span>{item.description || '未填写说明'}</span>
        </p>
      ))}
    </section>
  )
}
