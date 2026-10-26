'use client'

import { useState, useEffect, useCallback } from 'react'
import { api, type PaperListItem, type PaperSet, type PaperQuestion } from '@/lib/api'
import { PageHeader, ConfidentialBanner, SectionLabel, Badge, Alert, Spinner, EmptyState, ActiveSubjectBanner } from '@/components/ui'

export default function ReviewPage() {
  const [papers, setPapers] = useState<PaperListItem[]>([])
  const [loadingList, setLoadingList] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<PaperSet | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [approving, setApproving] = useState(false)
  const [msg, setMsg] = useState('')
  const [updatingSlot, setUpdatingSlot] = useState<string | null>(null)
  const [regeneratingSlot, setRegeneratingSlot] = useState<string | null>(null)
  const [editingSlot, setEditingSlot] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  const [showPreviewModal, setShowPreviewModal] = useState(false)

  const refreshPapers = useCallback(async () => {
    setLoadingList(true)
    try {
      const list = await api.listPapers()
      setPapers(list)
    } catch {
      // silently ignore
    } finally {
      setLoadingList(false)
    }
  }, [])

  // Fetch on mount
  useEffect(() => {
    refreshPapers()
  }, [refreshPapers])

  // Re-fetch whenever the user switches back to this tab or subject changes
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') refreshPapers()
    }
    const onSubjectChange = () => {
      setSelected(null)
      setDetail(null)
      refreshPapers()
    }
    document.addEventListener('visibilitychange', onVisible)
    if (typeof window !== 'undefined') {
      window.addEventListener('activeSubjectChanged', onSubjectChange)
    }
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      if (typeof window !== 'undefined') {
        window.removeEventListener('activeSubjectChanged', onSubjectChange)
      }
    }
  }, [refreshPapers])

  async function loadDetail(id: string) {
    setSelected(id)
    setDetail(null)
    setLoadingDetail(true)
    try {
      const p = await api.getPaper(id)
      setDetail(p)
    } catch (err: unknown) {
      setMsg(`❌ Failed to load paper details: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setLoadingDetail(false)
    }
  }

  async function handleApprove() {
    if (!selected) return
    setApproving(true)
    try {
      await api.approvePaper(selected)
      setMsg('✅ Paper set and all questions approved!')
      setPapers(p => p.map(x => x.set_id === selected ? { ...x, status: 'APPROVED' } : x))
      setDetail(d => d ? {
        ...d,
        status: 'APPROVED',
        questions: d.questions.map(q => ({ ...q, approval_status: 'APPROVED' })),
      } : null)
      setTimeout(() => setMsg(''), 4000)
    } catch (err: unknown) {
      setMsg(`❌ Approval failed: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setApproving(false)
    }
  }

  async function updateQ(slotId: string, status: string, text?: string) {
    if (!selected) return
    setUpdatingSlot(slotId)
    try {
      const updated = await api.updateQuestionStatus(selected, slotId, status, text)
      setDetail(d => {
        if (!d) return null
        const questions = d.questions.map(q => q.slot_id === slotId ? updated : q)
        const allApprovedNow = questions.length > 0 && questions.every(q => q.approval_status === 'APPROVED')
        return {
          ...d,
          status: allApprovedNow ? 'APPROVED' : d.status,
          questions,
        }
      })
      if (status === 'APPROVED') {
        setMsg(`✅ Question ${slotId} approved`)
      } else if (status === 'REJECTED') {
        setMsg(`❌ Question ${slotId} marked as rejected`)
      }
      setTimeout(() => setMsg(''), 3000)
    } catch (err: unknown) {
      setMsg(`❌ Failed to update question ${slotId}: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setUpdatingSlot(null)
      setEditingSlot(null)
      setEditText('')
    }
  }

  async function handleRegenerateQ(slotId: string) {
    if (!selected) return
    setRegeneratingSlot(slotId)
    try {
      const updated = await api.regenerateQuestion(selected, slotId)
      setDetail(d => d ? {
        ...d,
        questions: d.questions.map(q => q.slot_id === slotId ? updated : q)
      } : null)
      setMsg(`⚡ Question ${slotId} regenerated successfully!`)
      setTimeout(() => setMsg(''), 4000)
    } catch (err: unknown) {
      setMsg(`❌ Regeneration failed: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setRegeneratingSlot(null)
    }
  }

  function statusBadge(status: string) {
    if (status === 'APPROVED') return <Badge variant="green">✅ Approved</Badge>
    if (status === 'REJECTED') return <Badge variant="red">❌ Rejected</Badge>
    return <Badge variant="orange">⏳ Pending</Badge>
  }

  function paperStatusBadge(status: string) {
    const map: Record<string, string> = {
      APPROVED: 'green', EXPORTED: 'blue', GENERATED: 'orange', UNDER_REVIEW: 'orange', CHANGES_REQUESTED: 'red'
    }
    return <Badge variant={(map[status] ?? 'gray') as any}>{status.replace('_', ' ')}</Badge>
  }

  const pendingCount = detail?.questions.filter(q => q.approval_status === 'PENDING').length ?? 0
  const allApproved = Boolean(
    detail &&
    (detail.status === 'APPROVED' ||
      (detail.questions.length > 0 && detail.questions.every(q => q.approval_status === 'APPROVED')))
  )

  return (
    <>
      <PageHeader title="Faculty Review" subtitle="Review, approve and export generated question papers" />
      <ConfidentialBanner />

      <ActiveSubjectBanner showLink={false} />

      {msg && <Alert variant="success">{msg}</Alert>}

      <div className="flex gap-6" style={{ alignItems: 'flex-start' }}>
        {/* Paper list sidebar */}
        <div style={{ width: 260, flexShrink: 0 }}>
          <div className="flex justify-between items-center" style={{ marginBottom: '0.5rem' }}>
            <SectionLabel style={{ margin: 0 }}>Paper Sets</SectionLabel>
            <button
              className="btn btn-secondary btn-sm"
              onClick={refreshPapers}
              disabled={loadingList}
              title="Refresh paper list"
              style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem' }}
            >
              {loadingList ? <Spinner size={12} /> : '↺ Refresh'}
            </button>
          </div>
          {loadingList && papers.length === 0 ? (
            <div className="text-center" style={{ padding: '2rem' }}><Spinner size={22} /></div>
          ) : papers.length === 0 ? (
            <EmptyState icon="📄" title="No papers yet" sub="Generate papers first" />
          ) : (
            <div className="card" style={{ overflow: 'hidden' }}>
              {papers.map((p, i) => (
                <div key={p.set_id}
                  onClick={() => loadDetail(p.set_id)}
                  style={{
                    padding: '0.875rem 1.25rem',
                    borderBottom: i < papers.length - 1 ? '1px solid var(--border-light)' : 'none',
                    cursor: 'pointer',
                    background: selected === p.set_id ? 'var(--blue-light)' : 'transparent',
                    transition: 'background 0.15s',
                  }}>
                  <div className="font-semibold text-sm" style={{ color: selected === p.set_id ? 'var(--blue)' : 'var(--text)' }}>
                    {p.set_id}
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    {paperStatusBadge(p.status)}
                    <span className="text-xs text-muted">{p.question_count}q</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Paper detail */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {!selected && (
            <EmptyState icon="👈" title="Select a paper set" sub="Choose from the list on the left" />
          )}

          {loadingDetail && (
            <div className="text-center" style={{ padding: '3rem' }}><Spinner size={28} /></div>
          )}

          {detail && (
            <>
              {/* Header & Actions */}
              <div className="flex justify-between items-center mb-4">
                <div>
                  <div className="font-bold" style={{ fontSize: '1.25rem' }}>{detail.set_id}</div>
                  <div className="flex gap-2 mt-1">
                    {paperStatusBadge(detail.status)}
                    <Badge variant="gray">{detail.exam_type}</Badge>
                    <Badge variant="blue">{detail.question_count} questions</Badge>
                  </div>
                </div>
                <div className="flex gap-3 items-center">
                  {pendingCount > 0 && (
                    <span className="badge badge-orange">{pendingCount} pending</span>
                  )}
                  <button className="btn btn-secondary btn-sm"
                    onClick={() => setShowPreviewModal(true)}>
                    👁 View Paper
                  </button>
                  {allApproved ? (
                    <button className="btn btn-secondary btn-sm"
                      onClick={async () => {
                        try {
                          await api.downloadExport(detail.set_id, 'docx')
                        } catch (err: unknown) {
                          window.open(api.exportDocxUrl(detail.set_id))
                        }
                      }}
                      title="Download DOCX format">
                      📥 DOCX
                    </button>
                  ) : (
                    <button className="btn btn-secondary btn-sm"
                      disabled
                      style={{ opacity: 0.5, cursor: 'not-allowed' }}
                      title="All questions must be approved before downloading DOCX">
                      🔒 📥 DOCX
                    </button>
                  )}
                  {allApproved ? (
                    <button className="btn btn-secondary btn-sm"
                      onClick={async () => {
                        try {
                          await api.downloadExport(detail.set_id, 'pdf')
                        } catch (err: unknown) {
                          window.open(api.exportPdfUrl(detail.set_id))
                        }
                      }}
                      title="Download PDF format">
                      📄 PDF
                    </button>
                  ) : (
                    <button className="btn btn-secondary btn-sm"
                      disabled
                      style={{ opacity: 0.5, cursor: 'not-allowed' }}
                      title="All questions must be approved before downloading PDF">
                      🔒 📄 PDF
                    </button>
                  )}
                  <button className="btn btn-primary btn-sm" onClick={handleApprove}
                    disabled={approving || detail.status === 'APPROVED'}>
                    {approving ? <Spinner size={14} /> : detail.status === 'APPROVED' ? '✅ Approved' : 'Approve All'}
                  </button>
                </div>
              </div>

              {/* Approval status banner for downloads */}
              {!allApproved ? (
                <div style={{
                  marginBottom: '1rem', padding: '0.65rem 1rem', borderRadius: '8px',
                  background: 'rgba(234, 88, 12, 0.08)', border: '1px solid rgba(234, 88, 12, 0.25)',
                  fontSize: '0.825rem', color: 'var(--text)', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  gap: '1rem'
                }}>
                  <div className="flex items-center gap-2">
                    <span style={{ fontSize: '1.1rem' }}>🔒</span>
                    <span><strong>Downloads Locked:</strong> All questions must be approved by faculty before downloading the question paper ({pendingCount} pending review).</span>
                  </div>
                  <button className="btn btn-primary btn-sm" onClick={handleApprove} disabled={approving} style={{ flexShrink: 0 }}>
                    {approving ? <><Spinner size={12} />&nbsp;Approving…</> : '✓ Approve All Now'}
                  </button>
                </div>
              ) : (
                <div style={{
                  marginBottom: '1rem', padding: '0.65rem 1rem', borderRadius: '8px',
                  background: 'rgba(22, 163, 74, 0.08)', border: '1px solid rgba(22, 163, 74, 0.25)',
                  fontSize: '0.825rem', color: 'var(--green-text, #15803d)', display: 'flex', alignItems: 'center', gap: '0.5rem'
                }}>
                  <span>✅</span>
                  <span><strong>All questions approved!</strong> Question paper downloads (DOCX and PDF) are unlocked.</span>
                </div>
              )}

              {/* View Paper PDF Modal Overlay */}
              {showPreviewModal && (
                <div style={{
                  position: 'fixed', inset: 0, zIndex: 10000,
                  background: 'rgba(0, 0, 0, 0.75)', backdropFilter: 'blur(4px)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1.5rem'
                }} onClick={() => setShowPreviewModal(false)}>
                  <div className="card" style={{
                    width: '100%', maxWidth: '960px', height: '90vh',
                    display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: 0,
                    borderRadius: '16px', boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.35)',
                    position: 'relative', zIndex: 10001
                  }} onClick={e => e.stopPropagation()}>
                    <div style={{
                      padding: '1rem 1.5rem', background: 'var(--bg-secondary)', borderBottom: '1px solid var(--border-light)',
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      position: 'relative', zIndex: 10, flexShrink: 0
                    }}>
                      <div className="font-bold text-lg flex items-center gap-2" style={{ color: 'var(--text)' }}>
                        View Question Paper ({detail.set_id})
                      </div>
                      <button className="btn btn-secondary btn-sm"
                        onClick={() => setShowPreviewModal(false)}
                        aria-label="Close"
                        style={{
                          fontSize: '1.25rem',
                          fontWeight: 'bold',
                          lineHeight: 1,
                          padding: '0.35rem 0.65rem',
                          cursor: 'pointer',
                          zIndex: 20,
                          position: 'relative',
                          color: 'var(--text)',
                          borderColor: 'var(--border)'
                        }}>
                        ✕
                      </button>
                    </div>
                    <div style={{ flex: 1, background: '#525659', padding: 0, overflow: 'hidden', position: 'relative', zIndex: 1 }}>
                      <iframe
                        src={api.previewUrl(detail.set_id)}
                        title={`View Question Paper ${detail.set_id}`}
                        width="100%"
                        height="100%"
                        style={{ border: 'none', background: '#ffffff', display: 'block' }}
                      />
                    </div>
                    <div style={{
                      padding: '0.85rem 1.5rem', background: 'var(--bg-secondary)', borderTop: '1px solid var(--border-light)',
                      display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '1rem', flexShrink: 0,
                      position: 'relative', zIndex: 10
                    }}>
                      {allApproved ? (
                        <button className="btn btn-primary"
                          onClick={() => window.open(api.exportPdfUrl(detail.set_id))}
                          style={{ padding: '0.5rem 1.75rem', fontWeight: 600 }}>
                          📥 Download Question Paper (PDF)
                        </button>
                      ) : (
                        <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--orange)', fontWeight: 500 }}>
                          <span>🔒 Question paper download is locked until all questions are approved ({pendingCount} pending).</span>
                          <button className="btn btn-secondary btn-sm" disabled style={{ opacity: 0.5, cursor: 'not-allowed' }}>
                            🔒 Download Locked
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {/* Questions */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.875rem' }}>
                {detail.questions.map(q => (
                  <div key={q.slot_id} className="card card-pad" style={{ borderLeft: `3px solid ${
                    q.approval_status === 'APPROVED' ? 'var(--green)' :
                    q.approval_status === 'REJECTED' ? 'var(--red)' : 'var(--orange)'
                  }` }}>
                    <div className="flex justify-between items-start gap-4">
                      <div style={{ flex: 1 }}>
                        <div className="flex gap-2 mb-2">
                          <span className="font-semibold text-sm text-muted">{q.slot_id}</span>
                          {q.bloom_level === 'L2'
                            ? <Badge variant="green">L2</Badge>
                            : <Badge variant="orange">L3</Badge>}
                          {q.source.includes('BANK')
                            ? <Badge variant="blue">Bank</Badge>
                            : <Badge variant="purple">AI</Badge>}
                          <span className="text-xs text-muted">Ch {q.chapter_number} · {q.marks} marks</span>
                        </div>

                        {editingSlot === q.slot_id ? (
                          <div>
                            <textarea className="form-textarea" value={editText}
                              onChange={e => setEditText(e.target.value)} rows={3} />
                            <div className="flex gap-2 mt-2">
                              <button className="btn btn-primary btn-sm"
                                onClick={() => updateQ(q.slot_id, 'PENDING', editText)}
                                disabled={updatingSlot === q.slot_id}>
                                {updatingSlot === q.slot_id ? <Spinner size={12} /> : 'Save Edit'}
                              </button>
                              <button className="btn btn-secondary btn-sm" onClick={() => setEditingSlot(null)}>
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <p className="text-sm" style={{ lineHeight: 1.6 }}>{q.question_text}</p>
                        )}
                      </div>

                      {/* Actions */}
                      <div className="flex flex-col gap-2" style={{ flexShrink: 0 }}>
                        {statusBadge(q.approval_status)}
                        <button className="btn btn-sm"
                          style={{ background: 'var(--green-light)', color: 'var(--green-text)' }}
                          onClick={() => updateQ(q.slot_id, 'APPROVED')}
                          disabled={updatingSlot === q.slot_id || regeneratingSlot === q.slot_id || q.approval_status === 'APPROVED'}>
                          ✅ Approve
                        </button>
                        <button className="btn btn-secondary btn-sm"
                          onClick={() => handleRegenerateQ(q.slot_id)}
                          disabled={updatingSlot === q.slot_id || regeneratingSlot === q.slot_id}>
                          {regeneratingSlot === q.slot_id ? <><Spinner size={12} /> Regenerating...</> : '🔄 Regenerate Question'}
                        </button>
                        <button className="btn btn-sm btn-danger"
                          onClick={() => updateQ(q.slot_id, 'REJECTED')}
                          disabled={updatingSlot === q.slot_id || regeneratingSlot === q.slot_id || q.approval_status === 'REJECTED'}>
                          ❌ Reject
                        </button>
                        <button className="btn btn-secondary btn-sm"
                          onClick={() => { setEditingSlot(q.slot_id); setEditText(q.question_text) }}
                          disabled={updatingSlot === q.slot_id || regeneratingSlot === q.slot_id}>
                          ✏️ Edit
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  )
}
