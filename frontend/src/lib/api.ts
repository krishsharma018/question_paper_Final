/**
 * lib/api.ts — Typed API client for the Prashnopatra FastAPI backend.
 * Usage: import { api } from '@/lib/api'
 *
 * Subject isolation: every request automatically carries X-Subject-Id header.
 * Call api.setActiveSubject(id) when the active subject changes.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api'

// ── Active subject state ─────────────────────────────────────────────────────────────────────
let _activeSubjectId: string | null = null
let _abortController: AbortController | null = null

/** Set the active subject. Aborts all in-flight requests from the previous subject. */
export function setActiveSubject(id: string | null): void {
  if (id === _activeSubjectId) return
  // Abort any in-flight requests from old subject
  if (_abortController) {
    _abortController.abort()
  }
  _activeSubjectId = id
  _abortController = new AbortController()
  // Mirror to localStorage for instant restore
  if (typeof window !== 'undefined') {
    if (id) {
      localStorage.setItem('activeSubjectId', id)
    } else {
      localStorage.removeItem('activeSubjectId')
    }
  }
}

/** Get the currently active subject id. */
export function getActiveSubject(): string | null {
  return _activeSubjectId
}

/** Initialize from localStorage (call once on app boot). */
export function initActiveSubjectFromStorage(): string | null {
  if (typeof window === 'undefined') return null
  const stored = localStorage.getItem('activeSubjectId')
  if (stored && !_activeSubjectId) {
    _activeSubjectId = stored
    _abortController = new AbortController()
  }
  return _activeSubjectId
}

// ── HTTP helpers ────────────────────────────────────────────────────────────────────────────

function _subjectHeaders(): Record<string, string> {
  if (_activeSubjectId) {
    return { 'X-Subject-Id': _activeSubjectId }
  }
  return {}
}

/** Determines if a path is subject-scoped (and should be cancelled when active subject switches) */
function _isSubjectScoped(path: string): boolean {
  if (path === '/subjects' || path.startsWith('/subjects/active') || path.startsWith('/subjects/lp-parse') || path === '/health' || path.startsWith('/settings')) {
    return false
  }
  return true
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ..._subjectHeaders(),
    ...(init?.headers as Record<string, string> | undefined),
  }
  // Only attach subject _abortController if request is subject-scoped and no custom signal provided
  const signal = init?.signal !== undefined
    ? init.signal
    : (_isSubjectScoped(path) ? (_abortController?.signal ?? undefined) : undefined)

  const res = await fetch(`${BASE}${path}`, {
    signal,
    ...init,
    headers,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

async function upload<T>(path: string, file: File, fieldName = 'file'): Promise<T> {
  const fd = new FormData()
  fd.append(fieldName, file)
  // File uploads like lp-parse should not be cancelled by background subject switches
  const signal = _isSubjectScoped(path) ? (_abortController?.signal ?? undefined) : undefined
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    body: fd,
    signal,
    headers: _subjectHeaders(),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`Upload ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// ── Legacy Types ──────────────────────────────────────────────────────────────────────────

export interface Chapter {
  unit_number: number
  chapter_number: number
  title: string
  topics: string[]
}

export interface CourseInfo {
  course_name: string
  course_code: string
  department: string
  semester: string
  academic_year: string
}

export interface CourseStructure {
  chapters: Chapter[]
  raw_text_length: number
}

export interface LessonPlanExtraction {
  chapters: Chapter[]
  confidence: number
  warnings: string[]
  raw_text_preview: string
}

// ── Multi-subject Types ────────────────────────────────────────────────────────────────────────

export interface ChapterData {
  id?: number
  chapter_number: number
  title: string
  topics: string[]
}

export interface UnitData {
  id?: number
  unit_number: number
  title: string
  chapters: ChapterData[]
}

export interface MinorConfig {
  minor1: number[]   // unit numbers covered by Minor 1
  minor2: number[]   // unit numbers covered by Minor 2
}

export interface LpMetadata {
  confidence: number
  warnings: string[]
  source_filename: string
  raw_text_length: number
}

/** Marking scheme for one exam type (ISA-I, ISA-II, or ESA) */
export interface ExamScheme {
  exam_type: string                // "ISA-I" | "ISA-II" | "ESA"
  total_marks: number
  duration: string
  instructions: string
  total_questions: number
  questions_to_attempt: number
  marks_per_full_question: number
  sub_question_pattern: number[]   // e.g. [10, 5]
}

/** Three independently editable marking schemes */
export interface MarkingSchemes {
  isa1: ExamScheme | null
  isa2: ExamScheme | null
  esa:  ExamScheme | null
}

export interface SubjectListItem {
  id?: number
  subject_id: string
  course_name: string
  course_code: string
  department: string
  semester: string
  academic_year: string
  minor_configuration: Partial<MinorConfig>
  lp_metadata: Partial<LpMetadata> & { marking_schemes?: MarkingSchemes | null }
  marking_schemes?: MarkingSchemes | null
}

export interface SubjectDetail extends SubjectListItem {
  units: UnitData[]
  created_at?: string
  updated_at?: string
}

export interface SubjectIn {
  course_name: string
  course_code: string
  department: string
  semester: string
  academic_year: string
  units: { unit_number: number; title: string; chapters: { chapter_number: number; title: string; topics: string[] }[] }[]
  minor_configuration: MinorConfig
  lp_metadata?: { confidence: number; warnings: string[]; source_filename: string; raw_text_length: number } | null
  marking_schemes?: MarkingSchemes | null
}

/** LP parse response — not yet saved; frontend shows review screen first */
export interface ParsedUnit {
  unit_number: number
  unit_name: string
  chapters: { chapter_number: number; title: string; topics: string[] }[]
}

export interface LpParseResult {
  course_name: string | null
  course_code: string | null
  department: string | null
  semester: string | null
  academic_year: string | null
  units: ParsedUnit[]
  minor_configuration: MinorConfig
  marking_schemes: MarkingSchemes | null
  confidence: number
  warnings: string[]
  raw_text_preview: string
}

// ── Existing types (unchanged) ──────────────────────────────────────────────────────────────────

export interface Question {
  question_id: string
  unit_number: number
  chapter_number: number
  chapter_name: string
  bloom_level: string
  question_text: string
  marks: number
  model_answer?: string
  difficulty: string
  source: string
  approval_status: string
}

export interface Completeness {
  chapter_number: number
  l2_count: number
  l3_count: number
  total: number
  l2_ok: boolean
  l3_ok: boolean
  complete: boolean
}

export interface ImportResult {
  total_rows: number
  imported: number
  warnings: string[]
  errors: string[]
  chapter_completeness: Record<number, Record<string, number>>
}

export interface PaperQuestion {
  slot_id: string
  question_text: string
  bloom_level: string
  marks: number
  source: string
  chapter_number: number
  chapter_name: string
  unit_number: number
  approval_status: string
  model_answer?: string
}

export interface PaperSet {
  set_id: string
  exam_type: string
  status: string
  created_at?: string
  question_count: number
  questions: PaperQuestion[]
}

export interface PaperListItem {
  set_id: string
  exam_type: string
  status: string
  created_at?: string
  question_count: number
}

export interface ValidationFinding {
  rule_id: string
  severity: string
  message: string
  expected?: string
  actual?: string
  suggested_action?: string
  affected_questions: string[]
}

export interface ValidationReport {
  set_id: string
  exam_type: string
  status: string
  findings: ValidationFinding[]
  source_bank_count: number
  source_ai_count: number
  source_bank_percent: number
  source_ai_percent: number
  bloom_l2_count: number
  bloom_l3_count: number
  bloom_l2_percent: number
  bloom_l3_percent: number
  total_printed: number
  marks_total: number
}

export interface GenerateResponse {
  success: boolean
  paper_set_ids: string[]
  validation_reports: ValidationReport[]
  errors: string[]
  warnings: string[]
}

export interface Stats {
  question_bank: { total: number; l2: number; l3: number; completeness_pct: number }
  papers: { total: number; pending: number; approved: number; exported: number }
  chapter_completeness: Record<number, Record<string, number>>
}

export interface AuditLog {
  id: number
  action: string
  timestamp?: string
  paper_set_id?: string
  details?: unknown
}

// ── API client ───────────────────────────────────────────────────────────────────────────────

export const api = {
  health: () => request<{ status: string }>('/health'),

  // ── Legacy Course (backward compat) ──────────────────────────────────────────────────
  getCourseInfo: () => request<CourseInfo>('/course/info'),
  saveCourseInfo: (data: CourseInfo) => request<CourseInfo>('/course/info', { method: 'POST', body: JSON.stringify(data) }),
  getCourseStructure: () => request<CourseStructure>('/course/structure'),
  saveCourseStructure: (chapters: Chapter[]) => request<CourseStructure>('/course/structure', { method: 'POST', body: JSON.stringify({ chapters }) }),
  extractLessonPlan: (file: File) => upload<LessonPlanExtraction>('/course/lesson-plan/extract', file),

  // ── Multi-subject Management ──────────────────────────────────────────────────────
  listSubjects: () => request<SubjectListItem[]>('/subjects'),
  createSubject: (data: SubjectIn) => request<SubjectDetail>('/subjects', { method: 'POST', body: JSON.stringify(data) }),
  getSubject: (subjectId: string) => request<SubjectDetail>(`/subjects/${subjectId}`),
  updateSubject: (subjectId: string, data: SubjectIn) => request<SubjectDetail>(`/subjects/${subjectId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSubject: (subjectId: string) => request<{ ok: boolean; deleted: string }>(`/subjects/${subjectId}`, { method: 'DELETE' }),
  getActiveSubjectId: () => request<{ active_subject_id: string | null }>('/subjects/active'),
  setActiveSubjectId: (subjectId: string) => request<{ active_subject_id: string }>('/subjects/active', { method: 'POST', body: JSON.stringify({ subject_id: subjectId }) }),
  parseLP: (file: File) => upload<LpParseResult>('/subjects/lp-parse', file),

  // ── Subject Context ─────────────────────────────────────────────────────────────────────
  getSubjectContext: (subjectId: string) => request<Record<string, unknown>>(`/subjects/${subjectId}/context`),
  putSubjectContext: (subjectId: string, ctx: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/subjects/${subjectId}/context`, { method: 'PUT', body: JSON.stringify(ctx) }),

  // ── Questions ──────────────────────────────────────────────────────────────────────────
  getQuestions: (params?: { chapter?: number; bloom?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.chapter) qs.set('chapter', String(params.chapter))
    if (params?.bloom) qs.set('bloom', params.bloom)
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.offset) qs.set('offset', String(params.offset))
    return request<Question[]>(`/questions?${qs}`)
  },
  getCompleteness: () => request<Completeness[]>('/questions/completeness'),
  importQuestions: (file: File) => upload<ImportResult>('/questions/import', file),
  updateQuestion: (id: string, data: Partial<Question>) =>
    request<Question>(`/questions/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteQuestion: (id: string) =>
    request<{ ok: boolean }>(`/questions/${id}`, { method: 'DELETE' }),

  // ── Papers ───────────────────────────────────────────────────────────────────────────
  listPapers: () => request<PaperListItem[]>('/papers'),
  getPaper: (id: string) => request<PaperSet>(`/papers/${id}`),
  generatePapers: (data: {
    exam_type: string; num_sets: number; l2_percent: number; l3_percent: number;
    selected_chapters: number[]; random_seed?: number; tolerance_percent: number;
    academic_year: string; department: string; exclude_used_question_ids: boolean;
  }) => request<GenerateResponse>('/papers/generate', { method: 'POST', body: JSON.stringify(data) }),
  updateQuestionStatus: (setId: string, slotId: string, status: string, editedText?: string) =>
    request<PaperQuestion>(`/papers/${setId}/questions/${slotId}`, {
      method: 'PATCH', body: JSON.stringify({ status, edited_text: editedText }),
    }),
  regenerateQuestion: (setId: string, slotId: string) =>
    request<PaperQuestion>(`/papers/${setId}/questions/${slotId}/regenerate`, {
      method: 'POST',
    }),
  approvePaper: (setId: string) =>
    request<{ ok: boolean }>(`/papers/${setId}/approve`, { method: 'POST' }),
  getValidation: (setId: string) => request<ValidationReport>(`/papers/${setId}/validation`),
  exportDocxUrl: (setId: string, subjectId?: string) => {
    const sid = subjectId || _activeSubjectId
    return `${BASE}/papers/${setId}/export/docx${sid ? `?subject_id=${encodeURIComponent(sid)}` : ''}`
  },
  exportPdfUrl: (setId: string, subjectId?: string) => {
    const sid = subjectId || _activeSubjectId
    return `${BASE}/papers/${setId}/export/pdf${sid ? `?subject_id=${encodeURIComponent(sid)}` : ''}`
  },
  previewUrl: (setId: string, subjectId?: string) => {
    const sid = subjectId || _activeSubjectId
    return `${BASE}/papers/${setId}/preview${sid ? `?subject_id=${encodeURIComponent(sid)}` : ''}`
  },
  downloadExport: async (setId: string, format: 'docx' | 'pdf', filename?: string) => {
    const url = format === 'docx' ? api.exportDocxUrl(setId) : api.exportPdfUrl(setId)
    const res = await fetch(url, { headers: _subjectHeaders() })
    if (!res.ok) {
      const err = await res.text()
      throw new Error(`Export failed (${res.status}): ${err}`)
    }
    const blob = await res.blob()
    const blobUrl = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl
    a.download = filename || `${setId}.${format}`
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.URL.revokeObjectURL(blobUrl)
  },

  // ── Audit ────────────────────────────────────────────────────────────────────────────
  getStats: () => request<Stats>('/audit/stats'),
  getLogs: (limit = 20) => request<AuditLog[]>(`/audit/logs?limit=${limit}`),

  // ── Settings ─────────────────────────────────────────────────────────────────────────
  getLLMHealth: () => request<{ status: string; message: string; model: string }>('/settings/health'),
}
