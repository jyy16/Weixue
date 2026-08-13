/**
 * Demo API client — returns data from embedded JSON, no backend needed.
 * Used by the runtime 演示模式 (see config/mode.js) — e.g. GitHub Pages.
 *
 * Key: demo-data.json is exported directly from SQLite, where JSON fields
 * are stored as TEXT strings. The backend normally parses these via Pydantic,
 * so we must parse them here before returning to the frontend.
 */
import demoData from '../demo-data.json';
import {
  computePrepAnalytics, computePrepInsights, computeClassReport, DIM_LABELS,
} from '../utils/analytics';
import { normalizeScores, averageRating } from '../utils/ratings';

const _clone = (v) => JSON.parse(JSON.stringify(v));
const _pristine = demoData;
let _data = _clone(demoData);
const ok = (d) => Promise.resolve(d);

// Persist the demo dataset to localStorage so multiple windows (student windows,
// teacher reloads, a second workbench tab) share the SAME data. Reset clears it.
// v2：五维度重构后缓存结构变化，升版本让旧 localStorage 自动作废，避免新旧数据挤压。
const STORAGE_KEY = 'weixue-demo-data-v2';

function _persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      data: _data, status: _status, dialogue: _dialogue, suggestion: _lastSuggestion,
    }));
  } catch { /* quota/security errors ignored */ }
}

function _hydrate() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.data) _data = saved.data;
    if (saved.status) Object.assign(_status, saved.status);
    if (saved.dialogue) Object.assign(_dialogue, saved.dialogue);
    if (saved.suggestion) Object.assign(_lastSuggestion, saved.suggestion);
  } catch { /* corrupted storage ignored */ }
}

// Live-classroom simulation state (demo mode only).
const _status = {};    // { [responseId]: 'not_started'|'recording'|'submitted'|'processing'|'processed' }
const _dialogue = {};  // { [responseId]: [turn, ...] }
const _lastSuggestion = {}; // { [responseId]: {questions, scaffold_status, echo_risk, note} }

_hydrate();

const MOCK_SCORES = {
  position: 'A', material: 'A-', structure: 'B+', language: 'A-', perspective: 'B+',
};

const DEMO_SUGGESTIONS = [
  '你的理由和结论之间是不是缺了什么？要不要补充一下？',
  '如果换一个角度想，这件事还会有什么不同的看法？',
];

const DEMO_SUGGESTIONS_ECHO = [
  '可以换一种方式说说你的想法吗？比如如果你是这只狐狸，你会怎么做？',
  '你的理由和结论之间是不是缺了什么？要不要补充一下？',
];

// Demo transcript used by the simulated audio import (matches the 动物园 topics).
const DEMO_TRANSCRIPT =
  '我觉得应该把老鹰放回野外。因为老鹰本来就是天空的动物，关在动物园里就只能走来走去，很不自由。' +
  '我同意它康复后放走，但是要确认它真的能自己抓食物再放，不然它又会受伤。';

// ── JSON field parsers ──────────────────────────────────

function _jp(val) {
  if (!val) return val;
  if (typeof val !== 'string') return val;
  try { return JSON.parse(val); } catch { return val; }
}

function _parseResponse(r) {
  if (!r) return r;
  return {
    ...r,
    processing_status: r.processing_status || _status[r.id] || 'not_started',
    teacher_rating: r.teacher_rating || '',
    ai_dimension_scores: normalizeScores(_jp(r.ai_dimension_scores)),
    teacher_dimension_scores: normalizeScores(_jp(r.teacher_dimension_scores)),
    ai_reasoning: _jp(r.ai_reasoning),
    ai_extracted_features: _jp(r.ai_extracted_features),
    ai_suggested_tags: _jp(r.ai_suggested_tags),
    teacher_tags: _jp(r.teacher_tags),
  };
}

function _parseTopic(t) {
  if (!t) return t;
  return { ...t, reference_arguments: _jp(t.reference_arguments) };
}

function _parseCalibration(c) {
  if (!c) return c;
  return {
    ...c,
    ai_original_scores: _jp(c.ai_original_scores),
    teacher_final_scores: _jp(c.teacher_final_scores),
    modifications: _jp(c.modifications),
  };
}

function _parseTag(t) {
  if (!t) return t;
  return { ...t, topic_ids: _jp(t.topic_ids) };
}

// ── Courses ─────────────────────────────────────────────
export const getCourses = () => ok(_data.courses);
export const getCourse = (cid) => ok(_data.courses.find(c => c.id === cid));
export const createCourse = (data) => {
  const id = Math.max(0, ..._data.courses.map(c => c.id)) + 1;
  const c = { id, ...data, created_at: new Date().toISOString(), topic_count: 0, student_count: 0 };
  _data.courses.push(c);
  _persist();
  return ok(c);
};

// ── Topics ──────────────────────────────────────────────
export const getTopics = (cid) =>
  ok(_data.topics.filter(t => t.course_id === cid).map(_parseTopic));
export const createTopic = (cid, data) => {
  const courseTopics = _data.topics.filter(t => t.course_id === cid);
  const order = courseTopics.length ? Math.max(...courseTopics.map(t => t.order)) + 1 : 1;
  const t = {
    id: Math.max(0, ..._data.topics.map(x => x.id)) + 1,
    course_id: cid,
    order,
    rubric_template_id: null,
    ...data,
  };
  _data.topics.push(t);
  _persist();
  return ok(t);
};
export const updateTopic = (tid, data) => {
  const t = _data.topics.find(x => x.id === tid);
  if (t) Object.assign(t, data);
  _persist();
  return ok(t);
};
export const deleteTopic = (tid) => {
  _data.topics = _data.topics.filter(x => x.id !== tid);
  _data.responses = _data.responses.filter(r => r.topic_id !== tid);
  _persist();
  return ok({ ok: true, topic_id: tid });
};

// ── Students ────────────────────────────────────────────
export const getStudents = (cid) => ok(_data.students.filter(s => s.course_id === cid));
export const createStudentsBatch = (cid, students) => {
  const created = [];
  let nextId = Math.max(0, ..._data.students.map(s => s.id)) + 1;
  students.forEach(s => {
    if (_data.students.some(x => x.course_id === cid && x.name === s.name)) return;
    const st = {
      id: nextId++, course_id: cid, name: s.name, grade: s.grade,
      comment_draft: '', feishu_open_id: s.feishu_open_id || '',
      comment_delivery_status: 'not_sent', comment_delivery_error: '',
      comment_delivered_at: null,
    };
    _data.students.push(st);
    created.push(st);
  });
  _persist();
  return ok({ created, skipped: [] });
};
export const updateStudent = (sid, data) => {
  const s = _data.students.find(x => x.id === sid);
  if (s) {
    if (data.feishu_open_id !== undefined && data.feishu_open_id !== (s.feishu_open_id || '')) {
      Object.assign(s, {
        comment_delivery_status: 'not_sent', comment_delivery_error: '',
        comment_delivered_at: null,
      });
    }
    Object.assign(s, data);
  }
  _persist();
  return ok(s);
};
export const deleteStudent = (sid) => {
  _data.students = _data.students.filter(x => x.id !== sid);
  _data.responses = _data.responses.filter(r => r.student_id !== sid);
  _persist();
  return ok({ ok: true, student_id: sid });
};

// ── Responses ───────────────────────────────────────────
export const getResponses = (cid, studentId) => {
  const studentIds = new Set(
    _data.students.filter(student => student.course_id === cid).map(student => student.id),
  );
  let resps = _data.responses.map(_parseResponse).filter(response => studentIds.has(response.student_id));
  if (studentId) resps = resps.filter(r => r.student_id === studentId);
  return ok(resps);
};

export const getResponse = (rid) =>
  ok(_parseResponse(_data.responses.find(r => r.id === rid)));

export const deleteResponse = (rid) => {
  _data.responses = _data.responses.filter(r => r.id !== rid);
  return ok({ ok: true, response_id: rid });
};

/** Register a response object received from another tab (live status bus). */
export const registerResponse = (resp) => {
  if (!resp || !resp.id) return;
  const idx = _data.responses.findIndex(r => r.id === resp.id);
  if (idx >= 0) _data.responses[idx] = resp;
  else _data.responses.push(resp);
};

export const reviewResponse = (rid, data) => {
  const response = _data.responses.find(r => r.id === rid);
  if (!response) return Promise.reject(new Error('Response not found'));
  response.teacher_dimension_scores = data.dimension_scores || null;
  response.teacher_tags = data.tags || [];
  response.teacher_note = data.note || '';
  response.teacher_confidence_override = data.confidence_override || null;
  response.teacher_reviewed = true;
  response.teacher_rating = data.rating || '';
  response.processing_status = 'processed';
  _status[rid] = 'processed';
  _persist();
  return ok(_parseResponse(response));
};

export const importAudio = (cid, studentId, topicId, file, source) => {
  let resp = _data.responses.find(
    r => r.student_id === studentId && r.topic_id === topicId
  );
  if (!resp) {
    resp = { id: Math.max(0, ..._data.responses.map(r => r.id)) + 1, student_id: studentId, topic_id: topicId };
    _data.responses.push(resp);
  }
  resp.raw_text = DEMO_TRANSCRIPT;
  resp.source = source || 'audio';
  resp.cleaned_text = '';
  resp.ai_dimension_scores = null;
  resp.ai_confidence = 'uncertain';
  resp.teacher_reviewed = false;
  resp.teacher_rating = '';
  resp.processing_status = 'submitted';
  _status[resp.id] = 'submitted';
  // 首轮发言也要进对话轮次（与真实后端一致），否则学生端轮询/时间线会丢第一轮。
  if (!(_dialogue[resp.id] || []).some(t => t.role === 'student')) {
    (_dialogue[resp.id] ||= []).push({
      id: Date.now(), response_id: resp.id, role: 'student',
      content: DEMO_TRANSCRIPT, turn_type: '', created_at: new Date().toISOString(),
    });
  }
  _persist();
  return ok(_parseResponse(resp));
};
export const importText = (cid, studentId, topicId, text, source) => {
  let resp = _data.responses.find(r => r.student_id === studentId && r.topic_id === topicId);
  if (!resp) {
    resp = { id: Math.max(0, ..._data.responses.map(r => r.id)) + 1, student_id: studentId, topic_id: topicId };
    _data.responses.push(resp);
  }
  resp.raw_text = text;
  resp.source = source || 'manual';
  resp.cleaned_text = '';
  resp.ai_dimension_scores = null;
  resp.ai_confidence = 'uncertain';
  resp.teacher_reviewed = false;
  resp.teacher_rating = '';
  resp.processing_status = 'submitted';
  _status[resp.id] = 'submitted';
  // 首轮发言也要进对话轮次（与真实后端一致），否则学生端轮询/时间线会丢第一轮。
  if (!(_dialogue[resp.id] || []).some(t => t.role === 'student')) {
    (_dialogue[resp.id] ||= []).push({
      id: Date.now(), response_id: resp.id, role: 'student',
      content: text, turn_type: '', created_at: new Date().toISOString(),
    });
  }
  _persist();
  return ok(_parseResponse(resp));
};

// ── ASR settings (demo mode: choice is local-only; real providers need a backend) ──
const ASR_STORAGE_KEY = 'weixue-asr-provider-v1';
const DEMO_ASR_PROVIDERS = [
  { id: 'mock', label: '演示转写（mock）', ready: true, reason: '' },
  { id: 'qwen_asr', label: '百炼 qwen3-asr-flash（推荐）', ready: false, reason: '纯前端演示模式无真实 ASR，需连接后端' },
  { id: 'openai', label: 'OpenAI 兼容（whisper）', ready: false, reason: '纯前端演示模式无真实 ASR，需连接后端' },
  { id: 'dashscope', label: 'DashScope 百炼（paraformer）', ready: false, reason: '纯前端演示模式无真实 ASR，需连接后端' },
];

const _demoAsrSettings = () => {
  let provider = 'mock';
  try {
    provider = localStorage.getItem(ASR_STORAGE_KEY) || 'mock';
  } catch { /* storage unavailable */ }
  if (!DEMO_ASR_PROVIDERS.some(p => p.id === provider)) provider = 'mock';
  return ok({
    provider,
    model: '',
    api_key_configured: false,
    providers: DEMO_ASR_PROVIDERS,
    demo: true,
    demo_data_present: false,
  });
};

export const getAsrSettings = () => _demoAsrSettings();
export const setAsrProvider = (provider) => {
  try {
    localStorage.setItem(ASR_STORAGE_KEY, provider);
  } catch { /* storage unavailable */ }
  return _demoAsrSettings();
};

// ── In-app settings (demo mode: read-only, requires real backend) ──
export const getSettings = () => ok({
  items: {},
  llm_configured: false,
  asr_configured: false,
  feishu_configured: false,
  bitable_configured: false,
  demo: true,
});
export const updateSettings = () => ok({
  items: {},
  llm_configured: false,
  asr_configured: false,
  feishu_configured: false,
  bitable_configured: false,
  demo: true,
  message: '演示模式无后端，设置只能在连接后端的真实模式中修改。',
});
export const testSettings = () => ok({
  ok: false,
  detail: '演示模式无后端，无法测试配置。',
});

// ── AI Companion (demo simulation) ─────────────────────
export const updateResponseStatus = (rid, status) => {
  const resp = _data.responses.find(r => r.id === rid);
  if (!resp) return Promise.reject(new Error('Response not found'));
  _status[rid] = status;
  resp.processing_status = status;
  _persist();
  return ok(_parseResponse(resp));
};

export const getDialogue = (rid) => ok((_dialogue[rid] || []).map(t => ({ ...t })));

export const flashFeedback = (rid) => {
  const dialogue = _dialogue[rid] || [];
  const studentTurns = dialogue.filter(t => t.role === 'student');
  const last = studentTurns[studentTurns.length - 1];
  const text = (last?.content || '').trim();
  // 演示版轻量规则：有因果连接词则点出“用理由支撑”，否则通用肯定
  const feedback = /因为|所以|如果/.test(text)
    ? '你把自己的想法说出来，还用理由撑住了它，真棒！'
    : '你把自己的想法说出来啦，真棒！';
  return ok({ feedback });
};

export const finishDialogue = (rid, by) => {
  const resp = _data.responses.find(r => r.id === rid);
  if (resp) {
    resp.dialogue_finished = by;
    _persist();
  }
  return ok(_parseResponse(resp));
};

export const appendTurn = (rid, data) => {
  const resp = _data.responses.find(r => r.id === rid);
  if (!resp) return Promise.reject(new Error('Response not found'));
  const turn = {
    id: Date.now(),
    response_id: rid,
    role: data.role,
    content: data.content,
    turn_type: data.turn_type || '',
    created_at: new Date().toISOString(),
  };
  (_dialogue[rid] ||= []).push(turn);
  if (data.role === 'student') {
    const prev = resp.raw_text || '';
    resp.raw_text = prev ? `${prev}\n${data.content}` : data.content;
    resp.cleaned_text = '';
    resp.ai_dimension_scores = null;
    resp.ai_confidence = 'uncertain';
    resp.ai_reasoning = {};
    resp.ai_extracted_features = {};
    resp.ai_note = '';
    resp.ai_suggested_tags = [];
    resp.teacher_reviewed = false;
    resp.teacher_rating = '';
    resp.processing_status = 'submitted';
    _status[rid] = 'submitted';
  }
  _persist();
  return ok(_parseResponse(resp));
};

export const suggestTurn = (rid) => {
  const resp = _data.responses.find(r => r.id === rid);
  const studentTurns = (_dialogue[rid] || []).filter(t => t.role === 'student');
  const last = studentTurns[studentTurns.length - 1];
  const text = (last?.content || resp?.raw_text || '').trim();
  const echo = text.length <= 12 && ['是的', '对的', '对', '嗯', '好'].some(m => text.includes(m));
  const suggestion = {
    questions: echo ? DEMO_SUGGESTIONS_ECHO : DEMO_SUGGESTIONS,
    scaffold_status: echo ? 'echo_risk' : 'continue',
    echo_risk: echo,
    note: echo
      ? '学生疑似在复述问法，建议换一种更开放的方式再问。'
      : '回答有了雏形，可以追问理由与结论之间的连接。',
  };
  _lastSuggestion[rid] = suggestion;
  return ok(suggestion);
};

export const assessOne = (rid) => {
  const resp = _data.responses.find(r => r.id === rid);
  if (!resp) return Promise.reject(new Error('Response not found'));
  _status[rid] = 'processing';
  return new Promise((resolve) => {
    setTimeout(() => {
      const scores = resp.ai_dimension_scores || { ...MOCK_SCORES };
      resp.ai_dimension_scores = scores;
      resp.ai_confidence = 'certain_good';
      resp.ai_reasoning = Object.fromEntries(
        Object.entries(scores).map(([dim, rating]) => [dim, {
          evidence: '学生原话', reasoning: '演示环境的模拟推理', rating,
        }]),
      );
      resp.ai_suggested_tags = ['观点明确', '有理由'];
      resp.cleaned_text = resp.raw_text || '';
      resp.teacher_reviewed = false;
      resp.teacher_rating = '';
      resp.processing_status = 'processed';
      _status[rid] = 'processed';
      _persist();
      resolve(_parseResponse(resp));
    }, 1200);
  });
};

// ── Parent report (interface reserved) ─────────────────
export const getStudentReport = (sid) => {
  const st = _data.students.find(s => s.id === sid);
  const resps = _data.responses.filter(r => r.student_id === sid).map(_parseResponse);
  const latest = resps[resps.length - 1] || null;
  const scores = latest ? (latest.teacher_dimension_scores || latest.ai_dimension_scores || {}) : {};
  const topic = latest ? _data.topics.find(t => t.id === latest.topic_id) : null;
  let bestAnswer = null;
  let bestScore = -1;
  resps.forEach((r) => {
    const text = (r.cleaned_text || r.raw_text || '').trim();
    if (!text) return;
    const s = r.teacher_dimension_scores || r.ai_dimension_scores || {};
    const avg = averageRating(s);
    if (avg > 0 && avg > bestScore) {
      bestScore = avg;
      const t = _data.topics.find((x) => x.id === r.topic_id);
      bestAnswer = { topic_title: t?.title || '', text, score: avg };
    }
  });
  return ok({
    student_id: sid,
    name: st?.name || '',
    grade: st?.grade || 4,
    has_report: !!latest,
    topic_title: topic?.title || '',
    dimensions: scores,
    teacher_comment: st?.comment_draft || '',
    best_answer: bestAnswer,
    rating: latest?.teacher_rating || '',
    reviewed: !!latest?.teacher_reviewed,
  });
};

// ── Assessment (no-op in demo) ──────────────────────────
export const assessCourse = (cid) => ok({ assessed: 0, skipped: 0 });
export const getAssessmentProgress = (cid) =>
  ok({
    completed: _data.responses.length,
    total: _data.responses.length,
    active: false,
    llm_calls: 0,
    skipped: 0,
    errors: 0,
  });
export const resetCourse = (cid) => {
  _data = _clone(_pristine);
  Object.keys(_status).forEach(k => delete _status[k]);
  Object.keys(_dialogue).forEach(k => delete _dialogue[k]);
  Object.keys(_lastSuggestion).forEach(k => delete _lastSuggestion[k]);
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  return ok({ ok: true });
};

export const clearCourseResponses = (cid) => {
  const studentIds = new Set(
    _data.students.filter(s => s.course_id === cid).map(s => s.id),
  );
  const topicIds = new Set(
    _data.topics.filter(t => t.course_id === cid).map(t => t.id),
  );
  const removedIds = new Set(
    _data.responses
      .filter(r => studentIds.has(r.student_id) && topicIds.has(r.topic_id))
      .map(r => r.id),
  );
  _data.responses = _data.responses.filter(r => !removedIds.has(r.id));
  const keptIds = new Set(_data.responses.map(r => r.id));
  Object.keys(_status).forEach(k => { if (!keptIds.has(Number(k))) delete _status[k]; });
  Object.keys(_dialogue).forEach(k => { if (!keptIds.has(Number(k))) delete _dialogue[k]; });
  Object.keys(_lastSuggestion).forEach(k => { if (!keptIds.has(Number(k))) delete _lastSuggestion[k]; });
  // 备课辅助的讲评计划（顺序/备注/总结）一并清掉。
  try { localStorage.removeItem(`weixue-prep-plan-${cid}`); } catch { /* ignore */ }
  _persist();
  return ok({ ok: true, responses_cleared: removedIds.size });
};

// ── Comments ────────────────────────────────────────────
export const generateComment = (cid, studentId) => {
  const student = _data.students.find(s => s.id === studentId && s.course_id === cid);
  if (!student) return Promise.reject(new Error('Student not found'));
  // Regeneration starts a new delivery cycle even when the demo text itself is
  // unchanged, matching the real backend semantics.
  student.comment_delivery_status = 'not_sent';
  student.comment_delivery_error = '';
  student.comment_delivered_at = null;
  _persist();
  return ok({ student_id: studentId, draft: student.comment_draft || '' });
};
export const saveCommentDraft = (cid, studentId, draft) => {
  const student = _data.students.find(s => s.id === studentId && s.course_id === cid);
  if (!student) return Promise.reject(new Error('Student not found'));
  if (student.comment_draft !== draft) {
    student.comment_draft = draft;
    student.comment_delivery_status = 'not_sent';
    student.comment_delivery_error = '';
    student.comment_delivered_at = null;
  }
  _persist();
  return ok({ ok: true, student_id: studentId });
};
export const sendComment = (cid, studentId, draft) => {
  const student = _data.students.find(s => s.id === studentId && s.course_id === cid);
  if (!student) return Promise.reject(new Error('Student not found'));
  if (student.comment_draft !== draft) {
    student.comment_draft = draft;
    student.comment_delivery_status = 'not_sent';
    student.comment_delivery_error = '';
    student.comment_delivered_at = null;
  }
  _persist();
  return ok({
    ok: true,
    student_id: studentId,
    status: 'saved_pending_delivery',
    message: '评语已保存并标记待发送；飞书机器人发送通道将在后续联调中接入。',
  });
};
export const batchGenerateComments = (cid) => {
  const students = _data.students.filter(student => student.course_id === cid);
  students.forEach(student => {
    student.comment_delivery_status = 'not_sent';
    student.comment_delivery_error = '';
    student.comment_delivered_at = null;
  });
  _persist();
  return ok({
    results: students.map(student => ({
      student_id: student.id,
      student_name: student.name,
      draft: student.comment_draft || '',
      error: null,
    })),
  });
};

// ── Prep Analytics ──────────────────────────────────────
export const getPrepAnalytics = (cid) => {
  const students = _data.students.filter(student => student.course_id === cid);
  const topics = _data.topics.filter(topic => topic.course_id === cid).map(_parseTopic);
  const studentIds = new Set(students.map(student => student.id));
  const responses = _data.responses
    .map(_parseResponse)
    .filter(response => studentIds.has(response.student_id));
  return ok(computePrepAnalytics(students, topics, responses));
};

const _courseData = (cid) => {
  const students = _data.students.filter(student => student.course_id === cid);
  const topics = _data.topics.filter(topic => topic.course_id === cid).map(_parseTopic);
  const studentIds = new Set(students.map(student => student.id));
  const responses = _data.responses
    .map(_parseResponse)
    .filter(response => studentIds.has(response.student_id));
  return { students, topics, responses };
};

export const getPrepInsights = (cid) => {
  const { students, topics, responses } = _courseData(cid);
  return ok(computePrepInsights(students, topics, responses, cid));
};

// 演示模式没有 LLM：用与后端 _template_prep_summary 相同的规则生成确定性总结。
export const generatePrepSummary = (cid) => {
  const { students, topics, responses } = _courseData(cid);
  const insights = computePrepInsights(students, topics, responses, cid);
  const p = insights.participation;
  const tier = insights.tier_summary;
  const tierLabels = {
    basic: '基础层（1-2年级）', developing: '发展层（3-5年级）', advancing: '进阶层（6-7年级）',
  };
  const tierText = Object.entries(tier)
    .map(([k, v]) => `${tierLabels[k] || k} ${v.students}人、均分${v.avg_score}`)
    .join('；');
  let overview =
    `全班 ${p.students_answered} 名学生共提交 ${p.responses_total} 份作答，整体参与度良好。`
    + (tierText ? `各认知梯段表现：${tierText}。` : '')
    + '建议在讲评课上先肯定整体亮点，再针对薄弱维度做引导。';
  const quick = insights.quick_rating_counts || {};
  if ((quick.good || 0) + (quick.guide || 0) + (quick.echo || 0) > 0) {
    const quickParts = [
      quick.good ? `表达完整${quick.good}人` : '',
      quick.guide ? `需引导${quick.guide}人` : '',
      quick.echo ? `复述/未表达${quick.echo}人` : '',
    ].filter(Boolean).join('、');
    const base = `全班 ${p.students_answered} 名学生共提交 ${p.responses_total} 份作答，整体参与度良好。`
      + (quickParts ? `课堂即时评级（教师第一印象）：${quickParts}。` : '')
      + (tierText ? `各认知梯段表现：${tierText}。` : '')
      + '建议在讲评课上先肯定整体亮点，再针对薄弱维度做引导。';
    overview = base;
  }
  const weak = insights.problem_patterns;
  const problems = weak.length
    ? weak.slice(0, 4).map(w =>
        `- ${w.label}维度偏弱，影响 ${w.students_affected} 名学生、${w.topics_affected} 道辩题。`)
      .join('\n')
    : '- 当前未发现低于合格线的维度，可进入拓展与深度追问。';
  const suggestions =
    '- 讲评时先展示 1-2 份优质发言，说明好在哪里（结构或证据）；\n'
    + '- 针对最弱的 1-2 个维度设计当堂小练习，例如补充证据或换位思考；\n'
    + '- 关注低分学生名单，安排小组互评或个别追问。';
  const summary = {
    overview,
    problems,
    suggestions,
    generated_by: 'template',
    generated_at: new Date().toISOString(),
  };
  // 与真实后端一致：生成即持久化（保留已生成的分题总结）。
  const plan = { lesson_plan: [], notes: {}, confirmed: false, summary: {} };
  const stored = _readPlan(cid);
  if (stored) Object.assign(plan, stored);
  plan.summary = { ...(plan.summary || {}), ...summary };
  plan.savedAt = new Date().toISOString();
  try { localStorage.setItem(_planKey(cid), JSON.stringify(plan)); } catch { /* ignore */ }
  return ok(summary);
};

// ── Prep Plan (备课辅助 · 讲评计划) ─────────────────────
// Demo 模式没有后端，讲评计划沿用浏览器本地存储（跨窗口/刷新保留）。
const _planKey = (cid) => `weixue-prep-plan-${cid}`;

export const getPrepPlan = (cid) => {
  try {
    const raw = localStorage.getItem(_planKey(cid));
    if (raw) {
      const saved = JSON.parse(raw);
      const lessonPlan = Array.isArray(saved.lesson_plan)
        ? saved.lesson_plan
        : Array.isArray(saved.lessonPlan) ? saved.lessonPlan : [];
      if (lessonPlan.length || saved.notes) {
        return ok({
          course_id: cid,
          lesson_plan: lessonPlan,
          notes: saved.notes || {},
          confirmed: !!saved.confirmed,
          summary: saved.summary || {},
          updated_at: saved.savedAt || saved.updated_at || null,
        });
      }
    }
  } catch { /* corrupted storage ignored */ }
  return ok({ course_id: cid, lesson_plan: [], notes: {}, confirmed: false, summary: {}, updated_at: null });
};

export const savePrepPlan = (cid, data) => {
  let prev = null;
  try {
    const raw = localStorage.getItem(_planKey(cid));
    if (raw) prev = JSON.parse(raw);
  } catch { /* ignore */ }
  const plan = {
    lesson_plan: data.lesson_plan || [],
    notes: data.notes || {},
    confirmed: !!data.confirmed,
    summary: data.summary || (prev && prev.summary) || {},
    savedAt: new Date().toISOString(),
  };
  try {
    localStorage.setItem(_planKey(cid), JSON.stringify(plan));
  } catch { /* quota/security errors ignored */ }
  return ok({ course_id: cid, ...plan, updated_at: plan.savedAt });
};

export const pushPrepPlanCard = (cid) => ok({
  ok: true,
  status: 'pending_delivery',
  message: '演示模式：飞书机器人卡片仅在真实后端可用（待联调）。',
});

const _templateTopicSummary = (row, highlights) => {
  const dims = row && row.avg_dimension_scores
    ? Object.entries(row.avg_dimension_scores)
        .map(([d, v]) => `${DIM_LABELS[d] || d}${v}`)
        .join('、')
    : '暂无评分';
  const weak = (row && row.weak_dimensions) || [];
  const problems = weak.length
    ? weak.map(d => `- ${DIM_LABELS[d] || d}维度偏弱。`).join('\n')
    : '- 未发现低于合格线的维度，可进入拓展追问。';
  const hl = highlights.slice(0, 2);
  const suggestions = hl.length
    ? `- 讲评时可展示 ${hl.map(h => h.student_name).join('、')} 的优质发言。\n- 针对薄弱维度设计当堂小练习。`
    : '- 先带学生重新梳理本题的立场与理由，再逐步补充证据；\n- 可用反例启发学生换位思考。';
  return { overview: `本题各维度均分：${dims}。`, problems, suggestions, generated_by: 'template' };
};

const _readPlan = (cid) => {
  try {
    const raw = localStorage.getItem(_planKey(cid));
    if (raw) return JSON.parse(raw);
  } catch { /* ignore */ }
  return null;
};

export const generateTopicSummary = (cid, tid) => {
  const { students, topics, responses } = _courseData(cid);
  const insights = computePrepInsights(students, topics, responses, cid);
  const row = computePrepAnalytics(students, topics, responses)
    .find(r => r.topic_id === tid) || null;
  const hl = (insights.topic_highlights || []).filter(h => h.topic_id === tid).slice(0, 2);
  const summary = {
    ..._templateTopicSummary(row, hl),
    generated_at: new Date().toISOString(),
  };
  const plan = { lesson_plan: [], notes: {}, confirmed: false, summary: {} };
  const stored = _readPlan(cid);
  if (stored) Object.assign(plan, stored);
  const topicsMap = { ...((plan.summary && plan.summary.topics) || {}) };
  topicsMap[String(tid)] = summary;
  plan.summary = { ...(plan.summary || {}), topics: topicsMap };
  plan.savedAt = new Date().toISOString();
  try { localStorage.setItem(_planKey(cid), JSON.stringify(plan)); } catch { /* ignore */ }
  return ok(summary);
};

export const savePrepSummary = (cid, data) => {
  const plan = { lesson_plan: [], notes: {}, confirmed: false, summary: {} };
  const stored = _readPlan(cid);
  if (stored) Object.assign(plan, stored);
  const summary = plan.summary || {};
  if (data.topic_id) {
    const topicsMap = { ...(summary.topics || {}) };
    const cur = { ...(topicsMap[String(data.topic_id)] || {}) };
    ['overview', 'problems', 'suggestions'].forEach(k => {
      if (data[k] !== undefined) cur[k] = data[k];
    });
    cur.edited = true;
    topicsMap[String(data.topic_id)] = cur;
    summary.topics = topicsMap;
  } else {
    ['overview', 'problems', 'suggestions'].forEach(k => {
      if (data[k] !== undefined) summary[k] = data[k];
    });
    summary.edited = true;
  }
  plan.summary = summary;
  plan.savedAt = new Date().toISOString();
  try { localStorage.setItem(_planKey(cid), JSON.stringify(plan)); } catch { /* ignore */ }
  return ok(summary);
};

// ── Report ──────────────────────────────────────────────
export const getClassReport = (cid) => {
  const students = _data.students.filter(student => student.course_id === cid);
  const topics = _data.topics.filter(topic => topic.course_id === cid).map(_parseTopic);
  const studentIds = new Set(students.map(student => student.id));
  const responses = _data.responses.map(_parseResponse).filter(response => studentIds.has(response.student_id));
  const tags = _data.tags.filter(tag => tag.course_id === cid).map(_parseTag);
  return ok(computeClassReport(students, topics, responses, tags, cid));
};

// ── Tags ────────────────────────────────────────────────
export const getTags = (cid) =>
  ok(_data.tags.filter(t => t.course_id === cid).map(_parseTag));
export const createTag = (cid, name, source) => ok({ id: 999, name, source, use_count: 0 });
export const updateTag = (tid, data) => ok({ id: tid, ...data });
export const mergeTags = (keepId, mergeIds) => ok({ id: keepId });
export const deleteTag = (tid) => ok({ ok: true });

// ── Calibrations ────────────────────────────────────────
export const getCalibrations = (cid) =>
  ok((_data.calibrations || []).map(_parseCalibration));

// ── System mode (演示模式：无后端能力矩阵，动作直接成功) ──
export const getSystemMode = () => ok({
  mode: 'demo',
  demo_course_present: _data.courses.length > 0,
  asr_provider: 'mock',
  asr_ready: true,
  llm_configured: false,
  feishu_ready: false,
  bitable_ready: false,
});

export const setSystemModeAction = (action) => {
  if (action === 'enter_demo' && _data.courses.length === 0) {
    _data = _clone(demoData);
    _persist();
  }
  return ok({ ok: true, action });
};

// ── Feishu Bitable (演示模式：仅真实后端可见) ─────────────
export const getFeishuBitableStatus = () => ok({
  mode: 'deferred',
  configured: false,
  app_token: '',
  table_ids: {},
  bindings: {},
  message: '演示模式：飞书多维表格状态仅在真实后端可见',
});

export const syncFeishuBitable = () => ok({
  status: 'ok',
  synced: 0,
  skipped: 0,
  errors: [],
});

export const pullFeishuBitable = () => ok({
  configured: false,
  mode: 'deferred',
  tables: {},
  unmatched_remote: 0,
});
