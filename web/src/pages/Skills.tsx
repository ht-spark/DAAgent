import { useEffect, useRef, useState } from 'react'

import { deleteSkill, importSkill, listSkills, skillExportUrl, type SkillInfo } from '../api'
import ConfirmDialog from '../components/ConfirmDialog'
import { pushModalLayer } from '../modalLayer'

/** 管理项目目录中已安装的文件化 Skill。 */
export default function Skills() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
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
    if (!pendingDelete || deleting) return
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
          <p className="page-sub">查看、导入和删除可由用户手动加载的分析技能，点击卡片查看声明详情。</p>
        </div>
        <button
          type="button"
          className="skills-upload-button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {uploading ? '正在导入…' : '上传 Skill'}
        </button>
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

      {error && <p className="skills-error">{error}</p>}
      {loading ? (
        <p className="skills-empty">正在读取已安装技能…</p>
      ) : skills.length === 0 ? (
        <div className="skills-empty">
          <strong>还没有安装 Skill</strong>
          <span>上传包含 SKILL.md 的 ZIP 包后，它会安装到 data_analysis/skills。</span>
        </div>
      ) : (
        <div className="skills-grid">
          {skills.map((skill) => (
            <button
              type="button"
              className="skill-card"
              key={skill.skill_id}
              onClick={() => setDetailSkill(skill)}
            >
              <span className="skill-card-name">{skill.name}</span>
              <code className="skill-card-id">{skill.skill_id}</code>
              <span className="skill-card-desc">{skill.description}</span>
              <span className="skill-card-foot">
                <span className="skill-counts">
                  <span>资料 {skill.references.length}</span>
                  <span>资产 {skill.assets.length}</span>
                  <span>脚本 {skill.scripts.length}</span>
                </span>
                <span className="skill-card-more">查看详情</span>
              </span>
            </button>
          ))}
        </div>
      )}

      {detailSkill ? (
        <SkillDetailDialog
          skill={detailSkill}
          onClose={() => setDetailSkill(null)}
          onRequestDelete={() => setPendingDelete(detailSkill)}
        />
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`删除技能「${pendingDelete?.name ?? ''}」`}
        description="删除后该技能目录会从 data_analysis/skills 中移除，后续任务将无法再加载它；重新上传对应 ZIP 包即可恢复。"
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

/** 技能详情弹窗：展示完整声明内容，并提供导出与删除入口。 */
function SkillDetailDialog({
  skill,
  onClose,
  onRequestDelete,
}: {
  skill: SkillInfo
  onClose: () => void
  onRequestDelete: () => void
}) {
  // onClose 为内联箭头函数，用 ref 取最新值，避免每次渲染重新注册模态层
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // 压入模态层：统一处理页面滚动锁，且 Esc 只作用于最上层弹窗
  useEffect(() => pushModalLayer(() => onCloseRef.current()), [])

  const hasResources = Boolean(skill.references.length || skill.assets.length || skill.scripts.length)

  return (
    <div className="confirm-mask" onClick={onClose} role="presentation">
      <div
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
          <button type="button" className="confirm-ok is-danger" onClick={onRequestDelete}>
            删除技能
          </button>
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