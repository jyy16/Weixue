/**
 * API client — runtime dispatcher between 演示模式 and 真实模式.
 *
 * The mode comes from config/mode.js (the single source of truth), so
 * switching demo ⇄ real at runtime needs no rebuild. Every function keeps the
 * same signature for callers; only the underlying implementation is picked
 * per call. Demo implementations live exclusively in demoClient.js — there
 * must be no inline demo stubs here.
 */
import axios from 'axios';
import * as _demo from './demoClient';
import { resolveMode } from '../config/mode';

const api = axios.create({ baseURL: import.meta.env.VITE_API_BASE || '/api' });

/** Resolve demo implementation for the current mode; null ⇒ real backend. */
const _demoImpl = async () => {
  const mode = await resolveMode();
  return mode === 'real' ? null : _demo;
};

// ── Courses ─────────────────────────────────────────────
export const getCourses = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getCourses(...a) : api.get('/courses').then(r => r.data);
};
export const getCourse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getCourse(...a) : api.get(`/courses/${a[0]}`).then(r => r.data);
};
export const createCourse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.createCourse(...a) : api.post('/courses', a[0]).then(r => r.data);
};

// ── Topics ──────────────────────────────────────────────
export const getTopics = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getTopics(...a) : api.get(`/courses/${a[0]}/topics`).then(r => r.data);
};
export const createTopic = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.createTopic(...a) :
    api.post(`/courses/${a[0]}/topics`, a[1]).then(r => r.data);
};
export const updateTopic = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.updateTopic(...a) :
    api.put(`/topics/${a[0]}`, a[1]).then(r => r.data);
};
export const deleteTopic = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.deleteTopic(...a) :
    api.delete(`/topics/${a[0]}`).then(r => r.data);
};

// ── Students ────────────────────────────────────────────
export const getStudents = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getStudents(...a) : api.get(`/courses/${a[0]}/students`).then(r => r.data);
};
export const createStudentsBatch = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.createStudentsBatch(...a) :
    api.post(`/courses/${a[0]}/students/batch`, { students: a[1] }).then(r => r.data);
};
export const updateStudent = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.updateStudent(...a) :
    api.put(`/students/${a[0]}`, a[1]).then(r => r.data);
};
export const deleteStudent = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.deleteStudent(...a) :
    api.delete(`/students/${a[0]}`).then(r => r.data);
};

// ── Responses ───────────────────────────────────────────
export const getResponses = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getResponses(...a) :
    api.get(`/courses/${a[0]}/responses`, { params: a[1] ? { student_id: a[1] } : {} }).then(r => r.data);
};
export const getResponse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getResponse(...a) : api.get(`/responses/${a[0]}`).then(r => r.data);
};
export const deleteResponse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.deleteResponse(...a) :
    api.delete(`/responses/${a[0]}`).then(r => r.data);
};
export const reviewResponse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.reviewResponse(...a) :
    api.post(`/responses/${a[0]}/review`, a[1]).then(r => r.data);
};

// ── Audio import (ASR pipeline) ──────────────────────────
const AUDIO_EXT_BY_MIME = {
  'audio/webm': '.webm',
  'audio/mp4': '.mp4',
  'audio/ogg': '.ogg',
  'audio/wav': '.wav',
  'audio/mpeg': '.mp3',
  'audio/aac': '.aac',
};
export const importAudio = async (courseId, studentId, topicId, file, source) => {
  const demo = await _demoImpl();
  if (demo) return demo.importAudio(courseId, studentId, topicId, file, source);
  const fd = new FormData();
  fd.append('student_id', studentId);
  fd.append('topic_id', topicId);
  // Without an explicit filename MediaRecorder blobs arrive as "blob" and the
  // backend rejects them for missing extension — derive one from the MIME type.
  const ext = (file && AUDIO_EXT_BY_MIME[file.type]) || '.webm';
  fd.append('file', file, file?.name || `recording${ext}`);
  fd.append('source', source || 'audio');
  return api.post(`/courses/${courseId}/audio/import`, fd).then(r => r.data);
};
export const importText = async (courseId, studentId, topicId, text, source) => {
  const demo = await _demoImpl();
  return demo
    ? demo.importText(courseId, studentId, topicId, text, source)
    : api.post(`/courses/${courseId}/responses/text`, {
        student_id: studentId, topic_id: topicId, text, source: source || 'manual',
      }).then(r => r.data);
};
export const getAsrSettings = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getAsrSettings(...a) : api.get('/settings/asr').then(r => r.data);
};
export const setAsrProvider = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.setAsrProvider(...a) : api.post('/settings/asr', { provider: a[0] }).then(r => r.data);
};

// ── System mode (后端能力矩阵 + 演示数据动作) ────────────
export const getSystemMode = async () => {
  const demo = await _demoImpl();
  return demo
    ? demo.getSystemMode()
    : api.get('/settings/mode').then(r => r.data);
};
export const setSystemModeAction = async (action) => {
  const demo = await _demoImpl();
  return demo
    ? demo.setSystemModeAction(action)
    : api.post('/settings/mode', { action }).then(r => r.data);
};

// ── In-app settings (LLM / ASR / Feishu / Bitable) ─────
export const getSettings = async () => {
  const demo = await _demoImpl();
  return demo ? demo.getSettings() : api.get('/settings').then(r => r.data);
};
export const updateSettings = async (settings) => {
  const demo = await _demoImpl();
  return demo
    ? demo.updateSettings(settings)
    : api.put('/settings', { settings }).then(r => r.data);
};
export const testSettings = async (section) => {
  const demo = await _demoImpl();
  return demo
    ? demo.testSettings(section)
    : api.post(`/settings/test/${section}`).then(r => r.data);
};

// ── AI Companion (live classroom) ──────────────────────
const _withTimeout = (promise, ms, label) => {
  let timer;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} 超时（${ms / 1000}s）`)), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
};

export const suggestTurn = async (...a) => {
  const demo = await _demoImpl();
  if (demo) return demo.suggestTurn(...a);
  return _withTimeout(
    api.post(`/companion/${a[0]}/suggest-turn`).then(r => r.data),
    35000,
    'AI追问生成',
  );
};
export const appendTurn = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.appendTurn(...a) : api.post(`/responses/${a[0]}/turns`, a[1]).then(r => r.data);
};
export const getDialogue = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getDialogue(...a) : api.get(`/companion/${a[0]}`).then(r => r.data);
};
export const flashFeedback = async (...a) => {
  const demo = await _demoImpl();
  if (demo) return demo.flashFeedback(...a);
  return _withTimeout(
    api.post(`/companion/${a[0]}/feedback`).then(r => r.data),
    35000,
    '闪光反馈',
  );
};
export const updateResponseStatus = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.updateResponseStatus(...a) :
    api.patch(`/responses/${a[0]}/status`, { status: a[1] }).then(r => r.data);
};
export const finishDialogue = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.finishDialogue(...a) :
    api.post(`/responses/${a[0]}/dialogue-finish`, { by: a[1] }).then(r => r.data);
};
export const assessOne = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.assessOne(...a) : api.post(`/responses/${a[0]}/assess`).then(r => r.data);
};

// ── Parent report (interface reserved) ─────────────────
export const getStudentReport = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getStudentReport(...a) : api.get(`/students/${a[0]}/report`).then(r => r.data);
};

// ── Assessment ──────────────────────────────────────────
export const assessCourse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.assessCourse(...a) : api.post(`/courses/${a[0]}/assess`).then(r => r.data);
};
export const getAssessmentProgress = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getAssessmentProgress(...a) :
    api.get(`/courses/${a[0]}/assessment-progress`).then(r => r.data);
};
export const resetCourse = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.resetCourse(...a) : api.post(`/courses/${a[0]}/reset`).then(r => r.data);
};
export const clearCourseResponses = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.clearCourseResponses(...a) :
    api.post(`/courses/${a[0]}/responses/clear`).then(r => r.data);
};

/** Register a response object synced from another tab (demo mode only). */
export const registerDemoResponse = async (resp) => {
  const demo = await _demoImpl();
  if (demo) demo.registerResponse(resp);
};

// ── Comments ────────────────────────────────────────────
export const generateComment = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.generateComment(...a) :
    api.post(`/courses/${a[0]}/comments`, { student_id: a[1] }).then(r => r.data);
};
export const saveCommentDraft = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.saveCommentDraft(...a) :
    api.post(`/courses/${a[0]}/comments/save`, { student_id: a[1], draft: a[2] }).then(r => r.data);
};
export const sendComment = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.sendComment(...a) :
    api.post(`/courses/${a[0]}/comments/send`, { student_id: a[1], draft: a[2] }).then(r => r.data);
};
export const batchGenerateComments = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.batchGenerateComments(...a) :
    api.post(`/courses/${a[0]}/comments/batch`).then(r => r.data);
};

// ── Prep Analytics & Plan ───────────────────────────────
export const getPrepAnalytics = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getPrepAnalytics(...a) : api.get(`/courses/${a[0]}/prep`).then(r => r.data);
};
export const getPrepPlan = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getPrepPlan(...a) : api.get(`/courses/${a[0]}/prep/plan`).then(r => r.data);
};
export const savePrepPlan = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.savePrepPlan(...a) :
    api.put(`/courses/${a[0]}/prep/plan`, a[1]).then(r => r.data);
};
export const pushPrepPlanCard = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.pushPrepPlanCard(...a) :
    api.post(`/courses/${a[0]}/prep/plan/push`).then(r => r.data);
};
export const getPrepInsights = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getPrepInsights(...a) :
    api.get(`/courses/${a[0]}/prep/insights`).then(r => r.data);
};
export const generatePrepSummary = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.generatePrepSummary(...a) :
    api.post(`/courses/${a[0]}/prep/summary`).then(r => r.data);
};
export const generateTopicSummary = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.generateTopicSummary(...a) :
    api.post(`/courses/${a[0]}/prep/topics/${a[1]}/summary`).then(r => r.data);
};
export const savePrepSummary = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.savePrepSummary(...a) :
    api.put(`/courses/${a[0]}/prep/summary`, a[1]).then(r => r.data);
};

// ── Report ──────────────────────────────────────────────
export const getClassReport = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getClassReport(...a) : api.get(`/courses/${a[0]}/report`).then(r => r.data);
};

// ── Tags ────────────────────────────────────────────────
export const getTags = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getTags(...a) : api.get(`/courses/${a[0]}/tags`).then(r => r.data);
};
export const createTag = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.createTag(...a) :
    api.post(`/courses/${a[0]}/tags`, null, { params: { name: a[1], source: a[2] || 'base' } }).then(r => r.data);
};
export const updateTag = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.updateTag(...a) : api.put(`/tags/${a[0]}`, a[1]).then(r => r.data);
};
export const mergeTags = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.mergeTags(...a) :
    api.post('/tags/merge', { keep_id: a[0], merge_ids: a[1] }).then(r => r.data);
};
export const deleteTag = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.deleteTag(...a) : api.delete(`/tags/${a[0]}`).then(r => r.data);
};

// ── Calibrations ────────────────────────────────────────
export const getCalibrations = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getCalibrations(...a) :
    api.get(`/courses/${a[0]}/calibrations`).then(r => r.data);
};

// ── Feishu Bitable (多维表格同步) ───────────────────────
export const getFeishuBitableStatus = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.getFeishuBitableStatus(...a) : api.get('/feishu/bitable/status').then(r => r.data);
};
export const syncFeishuBitable = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.syncFeishuBitable(...a) :
    api.post('/feishu/bitable/sync', { course_id: a[0] }).then(r => r.data);
};
export const pullFeishuBitable = async (...a) => {
  const demo = await _demoImpl();
  return demo ? demo.pullFeishuBitable(...a) :
    api.post('/feishu/bitable/pull', { course_id: a[0] }).then(r => r.data);
};

export default api;
