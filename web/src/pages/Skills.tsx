import { useEffect, useRef, useState } from 'react'

import { importSkill, listSkills, skillExportUrl, type SkillInfo } from '../api'

/** 管理项目目录中已安装的文件化 Skill。 */
export default function Skills() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
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

  return (
    <div className="page modeling-tasks-page skills-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">技能</h1>
          <p className="page-sub">查看、导入和导出可由用户手动加载的分析技能。</p>
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
            <article className="skill-card" key={skill.skill_id}>
              <div className="skill-card-head">
                <div>
                  <h2>{skill.name}</h2>
                  <code>{skill.skill_id}</code>
                </div>
                <a href={skillExportUrl(skill.skill_id)} download={`${skill.skill_id}.zip`}>
                  导出
                </a>
              </div>
              <p>{skill.description}</p>
              <div className="skill-counts">
                <span>资料 {skill.references.length}</span>
                <span>资产 {skill.assets.length}</span>
                <span>脚本 {skill.scripts.length}</span>
              </div>
              <details>
                <summary>查看声明内容</summary>
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
              </details>
              <small className="skill-hash">版本 {skill.bundle_hash.slice(0, 12)}</small>
            </article>
          ))}
        </div>
      )}
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
