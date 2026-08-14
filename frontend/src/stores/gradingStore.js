import { create } from 'zustand';
import * as api from '../api/client';
import { subscribeStatus, publishStatus } from '../utils/statusBus';

let _liveSubscribedCid = null;
let _liveUnsubscribe = null;

// 模式/暂停持久化：演示环境里学生端是独立窗口，挂载时必须能读到教师端当前设置，
// 不能只依赖“切换时广播一次”（后打开的窗口/HMR 重挂载会错过）。
const _readLiveMode = () => {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('weixue-live-mode') === 'confirm' ? 'confirm' : 'auto';
  } catch { return 'auto'; }
};
const _readLivePaused = () => {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('weixue-live-paused') === '1';
  } catch { return false; }
};
const _persistLiveMode = (mode) => {
  try { if (typeof localStorage !== 'undefined') localStorage.setItem('weixue-live-mode', mode); } catch { /* ignore */ }
};
const _persistLivePaused = (paused) => {
  try { if (typeof localStorage !== 'undefined') localStorage.setItem('weixue-live-paused', paused ? '1' : '0'); } catch { /* ignore */ }
};

const useStore = create((set, get) => ({
  // ── State ──────────────────────────────────────────────────
  courseId: null,
  course: null,
  courses: [],
  topics: [],
  students: [],
  responses: {},        // { [studentId]: [response, ...] }
  tags: [],
  currentStudentIdx: 0,
  currentTab: 'grading',
  currentMode: 'live',          // 'live' (课堂) / 'workbench' (工作台)
  liveTopicId: null,
  liveStatus: {},               // { [responseId]: 'not_started'|'recording'|'submitted'|'processing'|'processed' }
  liveDialogue: {},             // { [responseId]: [turn, ...] }
  liveSuggestions: {},          // { [responseId]: {questions, scaffold_status, echo_risk, note} }
  liveRounds: {},               // { [responseId]: student-round count }
  liveAdopted: {},              // { [responseId]: adopted teacher question }
  liveBusy: {},                 // { [responseId]: true } while an action is in flight
  liveTranscripts: {},          // { [responseId]: 实时转写片段（学生正在说什么） }
  liveAiQuestions: {},          // { [responseId]: AI 自动追问的问题 }
  liveFinished: {},             // { [responseId]: 'student' | 'teacher' 谁结束了对话 }
  liveEchoRisk: {},             // { [responseId]: true 最近一轮有复述风险 }
  liveTurnPhase: {},            // { [responseId]: awaiting_teacher | awaiting_student | ai_processing | done }
  liveMode: _readLiveMode(),    // 'auto' 学生端直接追问 / 'confirm' 教师端确认后发送
  livePaused: _readLivePaused(),// 全局暂停 AI 伴学
  livePendingSuggestions: {},   // { [responseId]: {questions, echoRisk, note} } 确认模式待发送
  loading: false,
  assessing: false,     // was "grading"
  assessmentProgress: null,

  // ── Derived ────────────────────────────────────────────────
  currentStudent: () => {
    const { students, currentStudentIdx } = get();
    return students[currentStudentIdx] || null;
  },

  studentResponses: (studentId) => {
    return get().responses[studentId] || [];
  },

  findResponse: (responseId) => {
    const { responses } = get();
    let target = null;
    Object.values(responses).flat().forEach(r => { if (r.id === responseId) target = r; });
    return target;
  },

  // ── Actions ────────────────────────────────────────────────
  setTab: (tab) => set({ currentTab: tab }),
  setStudentIdx: (idx) => set({ currentStudentIdx: idx }),

  loadCourse: async (cid) => {
    set({ loading: true });
    try {
      const [course, topics, students, tags] = await Promise.all([
        api.getCourse(cid),
        api.getTopics(cid),
        api.getStudents(cid),
        api.getTags(cid),
      ]);
      // Load all responses
      const resps = await api.getResponses(cid);
      const respMap = {};
      resps.forEach(r => {
        if (!respMap[r.student_id]) respMap[r.student_id] = [];
        respMap[r.student_id].push(r);
      });
      set({ course, topics, students, tags, responses: respMap, courseId: cid, loading: false });
      get().initLiveStatus();
    } catch (e) {
      console.error('Failed to load course:', e);
      set({ loading: false });
    }
  },

  // Lightweight refresh used by the student manager while a Feishu delivery
  // may change asynchronously. Avoid reloading topics, responses and tags on
  // every poll, and ignore a late response after the user switches courses.
  refreshStudents: async (cid = get().courseId) => {
    if (!cid) return [];
    const students = await api.getStudents(cid);
    if (get().courseId === cid) set({ students });
    return students;
  },

  loadAllCourses: async () => {
    const list = await api.getCourses();
    set({ courses: list });
    if (list.length > 0 && !get().courseId) {
      await get().loadCourse(list[0].id);
    }
    return list;
  },

  selectCourse: async (cid) => {
    if (cid === get().courseId) return;
    set({ courseId: cid, currentStudentIdx: 0 });
    await get().loadCourse(cid);
  },

  // 演示/真实模式切换：两套数据完全独立，先清空当前会话状态再重新加载。
  resetForModeSwitch: async () => {
    set({
      courseId: null,
      currentStudentIdx: 0,
      responses: {},
      tags: [],
      assessing: false,
      assessmentProgress: null,
      liveStatus: {},
      liveDialogue: {},
      liveSuggestions: {},
      liveRounds: {},
      liveAdopted: {},
      liveTranscripts: {},
      liveAiQuestions: {},
      liveFinished: {},
      livePendingSuggestions: {},
      liveTurnPhase: {},
    });
    await get().loadAllCourses();
  },

  createCourse: async (data) => {
    const c = await api.createCourse(data);
    const list = await api.getCourses();
    set({ courses: list });
    await get().selectCourse(c.id);
    return c;
  },

  runAssessment: async () => {
    const cid = get().courseId;
    if (!cid) return;
    set({ assessing: true, assessmentProgress: null });

    try {
      await api.assessCourse(cid);
    } catch (e) {
      if (e.response?.status === 409) {
        console.warn('Assessment already in progress');
      } else {
        console.error('Failed to start assessment:', e);
        set({ assessing: false });
        return;
      }
    }

    // Poll progress every 500ms
    const pollInterval = setInterval(async () => {
      try {
        const p = await api.getAssessmentProgress(cid);
        set({ assessmentProgress: p });

        if (!p.active) {
          clearInterval(pollInterval);
          // Reload responses after assessment completes
          const resps = await api.getResponses(cid);
          const respMap = {};
          resps.forEach(r => {
            if (!respMap[r.student_id]) respMap[r.student_id] = [];
            respMap[r.student_id].push(r);
          });
          set({ responses: respMap, assessing: false });
        }
      } catch (e) {
        console.error('Progress poll failed:', e);
      }
    }, 500);
  },

  submitReview: async (responseId, data) => {
    const updated = await api.reviewResponse(responseId, data);
    // Update local state
    const { responses } = get();
    const newResps = { ...responses };
    for (const sid of Object.keys(newResps)) {
      newResps[sid] = newResps[sid].map(r => r.id === responseId ? updated : r);
    }
    set({ responses: newResps });
    return updated;
  },

  refreshTags: async () => {
    const cid = get().courseId;
    if (!cid) return;
    const tags = await api.getTags(cid);
    set({ tags });
  },

  resetAll: async () => {
    const cid = get().courseId;
    if (!cid) return;
    try {
      await api.resetCourse(cid);
      set({
        liveStatus: {}, liveDialogue: {}, liveSuggestions: {},
        liveRounds: {}, liveAdopted: {}, liveBusy: {},
        liveTranscripts: {}, liveAiQuestions: {}, liveFinished: {}, liveEchoRisk: {},
        livePendingSuggestions: {},
      });
      await get().loadCourse(cid);
    } catch (e) {
      console.error('Reset failed:', e);
    }
  },

  // 课堂调试：清除全班所有学生发言（作答/对话/评估/录音都删，保留学生与辩题）。
  clearLiveSpeech: async () => {
    const cid = get().courseId;
    if (!cid) return;
    await api.clearCourseResponses(cid);
    // 备课辅助的讲评计划也清掉（含本机 localStorage 兜底，避免旧计划复活）。
    try {
      localStorage.removeItem(`weixue-prep-plan-${cid}`);
    } catch { /* ignore */ }
    set({
      responses: {},
      assessing: false,
      assessmentProgress: null,
      liveStatus: {},
      liveDialogue: {},
      liveSuggestions: {},
      liveRounds: {},
      liveAdopted: {},
      liveTranscripts: {},
      liveAiQuestions: {},
      liveFinished: {},
      liveEchoRisk: {},
      liveTurnPhase: {},
      livePendingSuggestions: {},
    });
    await get().loadCourse(cid);
  },

  // ── Live classroom mode ─────────────────────────────────
  setMode: (mode) => set({ currentMode: mode }),
  setLiveTopic: (topicId) => set({ liveTopicId: topicId }),

  // 下一环节：切到下一个辩题，并清空本轮 live 会话状态（作答数据保留在库中）
  advanceLiveTopic: () => {
    const { topics, liveTopicId } = get();
    if (!topics || topics.length === 0) return;
    const idx = topics.findIndex(t => t.id === liveTopicId);
    if (idx < 0 || idx >= topics.length - 1) return;
    const next = topics[idx + 1];
    set({
      liveTopicId: next.id,
      liveStatus: {}, liveDialogue: {}, liveSuggestions: {}, liveRounds: {},
      liveAdopted: {}, liveBusy: {}, liveTranscripts: {}, liveAiQuestions: {},
      liveFinished: {}, liveEchoRisk: {}, livePendingSuggestions: {},
    });
  },

  initLiveStatus: () => {
    // liveStatus only tracks the CURRENT live session. Pre-existing historical
    // responses must NOT pre-fill it, otherwise the classroom would show
    // "已处理" for students who have not spoken in this session. Entries for
    // responses that no longer exist are dropped.
    const { responses, topics } = get();
    const validIds = new Set(Object.values(responses).flat().map(r => r.id));
    set(state => {
      const kept = {};
      Object.entries(state.liveStatus).forEach(([rid, s]) => {
        if (validIds.has(Number(rid))) kept[rid] = s;
      });
      return {
        liveStatus: kept,
        liveTopicId: get().liveTopicId || topics[0]?.id || null,
      };
    });
  },

  refreshResponses: async (cid) => {
    const resps = await api.getResponses(cid);
    const respMap = {};
    resps.forEach(r => {
      if (!respMap[r.student_id]) respMap[r.student_id] = [];
      respMap[r.student_id].push(r);
    });
    set({ responses: respMap, courseId: cid });
    get().initLiveStatus();
    return respMap;
  },

  subscribeLiveStatus: (cid) => {
    if (_liveSubscribedCid === cid) return;
    if (_liveUnsubscribe) { _liveUnsubscribe(); _liveUnsubscribe = null; }
    _liveSubscribedCid = cid;
    _liveUnsubscribe = subscribeStatus(cid, (evt) => {
      if (evt.courseId && evt.courseId !== cid) return;
      if (evt.response) {
        get()._upsertResponse(evt.response);
      }
      if (!evt.responseId) return;
      if (evt.transcript !== undefined) {
        set(state => ({ liveTranscripts: { ...state.liveTranscripts, [evt.responseId]: evt.transcript } }));
      }
      if (evt.type === 'teacher_question') {
        set(state => ({ liveAdopted: { ...state.liveAdopted, [evt.responseId]: evt.question || '' } }));
        set(state => ({ liveTurnPhase: { ...state.liveTurnPhase, [evt.responseId]: 'awaiting_student' } }));
        return;
      }
      if (evt.type === 'ai_question') {
        set(state => ({
          liveAiQuestions: { ...state.liveAiQuestions, [evt.responseId]: evt.question || '' },
          liveEchoRisk: { ...state.liveEchoRisk, [evt.responseId]: !!evt.echoRisk },
          liveTurnPhase: { ...state.liveTurnPhase, [evt.responseId]: 'awaiting_student' },
        }));
        return;
      }
      if (evt.type === 'student_finished' || evt.type === 'teacher_finished') {
        set(state => {
          const teacherFinished = evt.type === 'teacher_finished';
          const nextPending = { ...state.livePendingSuggestions };
          if (teacherFinished) delete nextPending[evt.responseId];
          return {
            liveFinished: {
              ...state.liveFinished,
              [evt.responseId]: teacherFinished ? 'teacher' : 'student',
            },
            liveTurnPhase: {
              ...state.liveTurnPhase,
              [evt.responseId]: teacherFinished ? 'done' : 'awaiting_teacher',
            },
            livePendingSuggestions: nextPending,
          };
        });
        return;
      }
      if (evt.type === 'live_mode') {
        set({ liveMode: evt.mode === 'confirm' ? 'confirm' : 'auto' });
        return;
      }
      if (evt.type === 'live_pause') {
        set({ livePaused: !!evt.paused });
        return;
      }
      if (evt.type === 'ai_suggestion_ready') {
        set(state => ({
          livePendingSuggestions: {
            ...state.livePendingSuggestions,
            [evt.responseId]: {
              questions: evt.questions || [],
              echoRisk: !!evt.echoRisk,
              note: evt.note || '',
            },
          },
          liveTurnPhase: { ...state.liveTurnPhase, [evt.responseId]: 'awaiting_teacher' },
        }));
        return;
      }
      if (evt.status === 'submitted') {
        set(state => ({ liveTurnPhase: { ...state.liveTurnPhase, [evt.responseId]: 'ai_processing' } }));
      } else if (evt.status === 'processed') {
        set(state => ({
          liveTurnPhase: {
            ...state.liveTurnPhase,
            [evt.responseId]: evt.response?.teacher_reviewed ? 'done' : 'awaiting_teacher',
          },
        }));
      }
      const prevRound = get().liveRounds[evt.responseId] || 0;
      const nextRound = evt.round || prevRound;
      set(state => ({
        liveStatus: { ...state.liveStatus, [evt.responseId]: evt.status },
        liveRounds: { ...state.liveRounds, [evt.responseId]: Math.max(prevRound, nextRound) },
      }));
      if (nextRound > prevRound) {
        set(state => ({ liveAdopted: { ...state.liveAdopted, [evt.responseId]: '' } }));
      }
      if (evt.status === 'submitted') {
        // 学生端 AI 自动追问（StudentWindow.autoAsk）已负责生成下一问，
        // 教师端这里只刷新对话历史，避免两处重复生成导致“错位”。
        setTimeout(() => { get().loadDialogue(evt.responseId); }, 250);
      }
    });
  },

  setLiveStatus: async (responseId, status, response) => {
    set(state => ({ liveStatus: { ...state.liveStatus, [responseId]: status } }));
    const cid = get().courseId;
    if (response) get()._upsertResponse(response);
    publishStatus(cid, { responseId, status, response: response || null });
    try {
      await api.updateResponseStatus(responseId, status);
    } catch (e) {
      console.warn('updateResponseStatus failed (demo fallback ok):', e);
    }
  },

  loadDialogue: async (responseId) => {
    try {
      const turns = await api.getDialogue(responseId);
      set(state => ({ liveDialogue: { ...state.liveDialogue, [responseId]: turns } }));
      return turns;
    } catch (e) {
      console.warn('loadDialogue failed:', e);
      return [];
    }
  },

  suggestTurnFor: async (responseId) => {
    set(state => ({ liveBusy: { ...state.liveBusy, [responseId]: true } }));
    try {
      const suggestion = await api.suggestTurn(responseId);
      set(state => ({ liveSuggestions: { ...state.liveSuggestions, [responseId]: suggestion } }));
      return suggestion;
    } catch (e) {
      console.warn('suggestTurn failed:', e);
      return null;
    } finally {
      set(state => ({ liveBusy: { ...state.liveBusy, [responseId]: false } }));
    }
  },

  adoptSuggestion: async (responseId, question) => {
    try {
      const updated = await api.appendTurn(responseId, { role: 'teacher', content: question, turn_type: 'scaffold' });
      get()._upsertResponse(updated);
      set(state => ({ liveAdopted: { ...state.liveAdopted, [responseId]: question } }));
      publishStatus(get().courseId, {
        responseId, status: 'submitted', type: 'teacher_question', question,
      });
      await get().loadDialogue(responseId);
      return updated;
    } catch (e) {
      console.warn('adoptSuggestion failed:', e);
      return null;
    }
  },

  finishLiveDialogue: async (responseId, by = 'teacher') => {
    set(state => {
      const nextPending = { ...state.livePendingSuggestions };
      delete nextPending[responseId];
      return {
        liveFinished: { ...state.liveFinished, [responseId]: by },
        liveTurnPhase: { ...state.liveTurnPhase, [responseId]: 'done' },
        livePendingSuggestions: nextPending,
      };
    });
    publishStatus(get().courseId, {
      responseId,
      type: by === 'teacher' ? 'teacher_finished' : 'student_finished',
    });
    try {
      await api.finishDialogue(responseId, by);
    } catch (e) {
      console.warn('finishDialogue persist failed:', e);
    }
  },

  setLiveMode: (mode) => {
    const next = mode === 'confirm' ? 'confirm' : 'auto';
    set({ liveMode: next });
    _persistLiveMode(next);
    publishStatus(get().courseId, { type: 'live_mode', mode: next });
  },

  togglePause: () => {
    const paused = !get().livePaused;
    set({ livePaused: paused });
    _persistLivePaused(paused);
    publishStatus(get().courseId, { type: 'live_pause', paused });
  },

  sendAiSuggestion: async (responseId, question) => {
    try {
      const updated = await api.appendTurn(responseId, { role: 'ai_suggestion', content: question, turn_type: 'scaffold' });
      get()._upsertResponse(updated);
      set(state => {
        const next = { ...state.livePendingSuggestions };
        delete next[responseId];
        return { livePendingSuggestions: next };
      });
      publishStatus(get().courseId, { responseId, type: 'ai_question', question });
      await get().loadDialogue(responseId);
      return updated;
    } catch (e) {
      console.warn('sendAiSuggestion failed:', e);
      return null;
    }
  },

  ignoreSuggestion: (responseId) => {
    set(state => {
      const next = { ...state.livePendingSuggestions };
      delete next[responseId];
      return { livePendingSuggestions: next };
    });
  },

  appendStudentTurn: async (responseId, content) => {
    try {
      const updated = await api.appendTurn(responseId, { role: 'student', content, turn_type: '' });
      get()._upsertResponse(updated);
      await get().setLiveStatus(responseId, 'submitted');
      await get().loadDialogue(responseId);
      return updated;
    } catch (e) {
      console.warn('appendStudentTurn failed:', e);
      return null;
    }
  },

  assessLive: async (responseId) => {
    set(state => ({ liveBusy: { ...state.liveBusy, [responseId]: true } }));
    await get().setLiveStatus(responseId, 'processing');
    try {
      const updated = await api.assessOne(responseId);
      get()._upsertResponse(updated);
      // Trust the backend status: it returns 'submitted' (retryable) when the
      // LLM produced no scores, so failures stay visible instead of green.
      await get().setLiveStatus(responseId, updated?.processing_status || 'processed', updated);
      await get().loadDialogue(responseId);
      return updated;
    } catch (e) {
      console.warn('assessOne failed:', e);
      await get().setLiveStatus(responseId, 'submitted');
      return null;
    } finally {
      set(state => ({ liveBusy: { ...state.liveBusy, [responseId]: false } }));
    }
  },

  reviewLive: async (responseId, { rating, note }) => {
    const { responses } = get();
    let target = null;
    Object.values(responses).flat().forEach(r => { if (r.id === responseId) target = r; });
    if (!target) return null;
    const updated = await get().submitReview(responseId, {
      dimension_scores: target.teacher_dimension_scores || target.ai_dimension_scores || {},
      tags: target.teacher_tags || target.ai_suggested_tags || [],
      note: note || target.teacher_note || '',
      rating: rating || '',
    });
    await get().setLiveStatus(responseId, 'processed', updated);
    set(state => ({ liveTurnPhase: { ...state.liveTurnPhase, [responseId]: 'done' } }));
    return updated;
  },

  quickRateLive: async (responseId, { rating, note }) => {
    // 推给 AI 评估之前的“当场判断”：只记录 teacher_rating / teacher_note，
    // 不标记 teacher_reviewed（正式五维批改仍在评估页进行）。
    try {
      const updated = await api.quickRating(responseId, { rating: rating || '', note: note || '' });
      get()._upsertResponse(updated);
      return updated;
    } catch (e) {
      console.warn('quickRating failed:', e);
      return null;
    }
  },


  openStudentWindow: (studentId) => {
    const { courseId, liveTopicId } = get();
    const url = `${window.location.pathname}#/student/${studentId}?course=${courseId || ''}&topic=${liveTopicId || ''}`;
    window.open(url, `student-${studentId}`, 'width=520,height=760');
  },

  _upsertResponse: (updated) => {
    api.registerDemoResponse(updated);
    const { responses } = get();
    const newResps = { ...responses };
    for (const sid of Object.keys(newResps)) {
      newResps[sid] = newResps[sid].map(r => (r.id === updated.id ? updated : r));
    }
    if (!Object.values(newResps).flat().some(r => r.id === updated.id)) {
      const list = newResps[updated.student_id] || [];
      newResps[updated.student_id] = [...list, updated];
    }
    // NOTE: do NOT auto-set liveStatus here. A response that exists in the data
    // layer but was not touched during this live session is HISTORY; the
    // classroom card stays 未发言 until a live event touches it.
    set({ responses: newResps });
  },
}));

export default useStore;
