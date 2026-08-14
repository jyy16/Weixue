import { useEffect, useState } from 'react';
import useStore from '../stores/gradingStore';
import { ratingToNumber, passLineForGrade } from '../utils/ratings';

const STATUS_META = {
  not_started: { label: '未发言', cls: 'bg-slate-100 text-slate-500', dot: 'bg-slate-300' },
  recording: { label: '正在发言', cls: 'bg-red-100 text-red-600', dot: 'bg-red-500 animate-pulse' },
  submitted: { label: '已发言', cls: 'bg-blue-100 text-blue-600', dot: 'bg-blue-500' },
  processing: { label: '处理中', cls: 'bg-amber-100 text-amber-600', dot: 'bg-amber-500 animate-pulse' },
  processed: { label: '已处理', cls: 'bg-green-100 text-green-700', dot: 'bg-green-500' },
};

const RATING_META = {
  good: { label: '👍 表达完整', cls: 'bg-green-600 text-white hover:bg-green-700' },
  guide: { label: '➕ 需引导', cls: 'bg-amber-500 text-white hover:bg-amber-600' },
  echo: { label: '⚠️ 复述/未表达', cls: 'bg-red-500 text-white hover:bg-red-600' },
};

export default function LiveCockpit() {
  const store = useStore();
  const {
    course, students, topics, responses, liveTopicId,
    liveStatus, liveSuggestions, liveDialogue, liveAdopted, liveBusy,
    liveTranscripts, liveAiQuestions, liveFinished, liveEchoRisk,
    liveMode, livePaused, livePendingSuggestions, liveTurnPhase,
  } = store;
  const [noteDrafts, setNoteDrafts] = useState({});
  const [askDrafts, setAskDrafts] = useState({});
  const [expanded, setExpanded] = useState({});
  const [focusId, setFocusId] = useState(null);

  useEffect(() => {
    if (course?.id) store.subscribeLiveStatus(course.id);
  }, [course?.id]);

  // 无论学生处于哪个阶段（含已处理/历史作答），都从后端加载完整对话轮次，
  // 避免退化成“只有学生、看不出轮次”的纯文本。
  useEffect(() => {
    if (!course?.id) return;
    const t = topics.find(x => x.id === liveTopicId) || topics[0] || null;
    if (!t) return;
    const fId = focusId ?? students[0]?.id ?? null;
    if (fId == null) return;
    const resp = (responses[fId] || []).find(r => r.topic_id === t.id);
    if (resp?.id) store.loadDialogue(resp.id);
  }, [course?.id, liveTopicId, focusId, students, topics, responses]);

  const topic = topics.find(t => t.id === liveTopicId) || topics[0] || null;

  const respFor = (studentId) => {
    const list = responses[studentId] || [];
    return list.find(r => r.topic_id === topic?.id) || null;
  };

  const statusOf = (studentId) => {
    const r = respFor(studentId);
    if (!r) return 'not_started';
    // liveStatus only tracks the current live session. A response that exists
    // but was never touched this session is HISTORY → the card stays 未发言.
    if (liveStatus[r.id]) return liveStatus[r.id];
    // 刷新/回访时 liveStatus 为空：按持久化状态回推，保证历史作答仍可查看与评价。
    if (r.teacher_reviewed) return 'processed';
    if (r.processing_status === 'processed' || r.ai_dimension_scores) return 'processed';
    if (r.processing_status === 'submitted' || r.raw_text) return 'submitted';
    return 'not_started';
  };

  const studentTurnCount = (rid) =>
    (liveDialogue[rid] || []).filter(t => t.role === 'student').length;

  // 需要老师出手的信号（按优先级排序展示）
  const studentSignals = (student) => {
    const resp = respFor(student.id);
    const status = statusOf(student.id);
    const signals = [];
    if (resp && (liveFinished[resp.id] || resp.dialogue_finished) && status !== 'processed') {
      signals.push({ key: 'pending', label: '待评估', cls: 'bg-red-100 text-red-700', score: 0 });
    }
    if (resp && liveEchoRisk[resp.id] && status !== 'processed') {
      signals.push({ key: 'echo', label: '复述风险', cls: 'bg-red-100 text-red-600', score: 1 });
    }
    if (resp && studentTurnCount(resp.id) >= 3 && status !== 'processed') {
      signals.push({ key: 'max3', label: '3轮已满', cls: 'bg-yellow-100 text-yellow-700', score: 2 });
    }
    if (status === 'processing') {
      signals.push({ key: 'processing', label: '处理中', cls: 'bg-amber-100 text-amber-600', score: 3 });
    }
    return signals;
  };

  const studentPriority = (student) => {
    const sig = studentSignals(student);
    if (sig.length > 0) return Math.min(...sig.map(s => s.score));
    const status = statusOf(student.id);
    if (status === 'recording') return 4;
    if (status === 'submitted') return 5;
    return 6;
  };

  const spokenCount = students.filter(s => statusOf(s.id) !== 'not_started').length;
  // 推给 AI 评估后老师侧流程即结束：课堂统计以“已处理”为准。
  const doneCount = students.filter(s => statusOf(s.id) === 'processed').length;
  // 达标 = 已处理且均分 ≥ 该生年级合格线（1-3年级 ≥2.5，4-6年级及以上 ≥3.0）。
  const passCount = students.filter(s => {
    const r = respFor(s.id);
    if (!r || statusOf(s.id) !== 'processed') return false;
    const scores = r.teacher_dimension_scores || r.ai_dimension_scores || {};
    const vals = Object.values(scores).map(ratingToNumber).filter(v => v !== null);
    const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
    return avg > 0 && avg >= passLineForGrade(s.grade);
  }).length;

  const handleAdopt = async (resp, question) => {
    await store.adoptSuggestion(resp.id, question);
  };

  const handleAssess = async (resp) => {
    await store.assessLive(resp.id);
  };

  const handleQuickRate = async (resp, rating) => {
    await store.quickRateLive(resp.id, { rating, note: noteDrafts[resp.id] || '' });
  };

  const openStudent = (studentId) => store.openStudentWindow(studentId);

  const handleClearSpeech = async () => {
    if (!window.confirm('清除全班当前所有发言？所有作答、对话和评估都会被删除（学生与辩题保留），此操作不可恢复。')) return;
    try {
      await store.clearLiveSpeech();
    } catch (e) {
      window.alert(`清除失败：${e?.response?.data?.detail || e?.message || '未知错误'}`);
    }
  };

  const renderTeacherAsk = (resp) => {
    const adoptedQ = liveAdopted[resp.id];
    const finished = liveFinished[resp.id] || resp.dialogue_finished || null;
    const turns = studentTurnCount(resp.id);
    const atLimit = turns >= 3;
    return (
      <div className="mt-3 rounded-xl border border-indigo-200 bg-indigo-50 p-3">
        <div className="text-xs font-semibold text-indigo-700 mb-1.5">老师可随时追问</div>
        {adoptedQ ? (
          <div className="text-xs text-green-700 bg-white rounded-lg p-2 border border-green-200">
            已发问：{adoptedQ}
          </div>
        ) : (
          <div className="flex gap-1.5">
            <input
              value={askDrafts[resp.id] || ''}
              onChange={e => setAskDrafts(prev => ({ ...prev, [resp.id]: e.target.value }))}
              placeholder="给这个学生发一句话…"
              className="flex-1 min-w-0 text-xs border border-indigo-200 rounded-lg px-2 py-1.5 outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
            />
            <button
              onClick={() => {
                const q = (askDrafts[resp.id] || '').trim();
                if (!q) return;
                handleAdopt(resp, q);
                setAskDrafts(prev => ({ ...prev, [resp.id]: '' }));
              }}
              disabled={liveBusy[resp.id]}
              className="shrink-0 text-[11px] font-medium bg-indigo-600 text-white rounded-md px-2.5 py-1.5 hover:bg-indigo-700 disabled:opacity-40"
            >
              发问
            </button>
          </div>
        )}
        <div className="mt-2 flex gap-2">
          {!finished && (
            <button
              onClick={() => store.finishLiveDialogue(resp.id, 'teacher')}
              disabled={liveBusy[resp.id]}
              className="flex-1 text-[11px] font-medium text-indigo-600 border border-indigo-300 rounded-md py-1.5 hover:bg-indigo-100 disabled:opacity-40"
            >
              ⏹ 结束对话
            </button>
          )}
          <button
            onClick={() => handleAssess(resp)}
            disabled={liveBusy[resp.id]}
            className="flex-1 text-[11px] font-medium bg-indigo-600 text-white rounded-md py-1.5 hover:bg-indigo-700 disabled:opacity-40"
          >
            {atLimit ? '已达 3 轮，直接评估' : '直接评估'}
          </button>
        </div>
      </div>
    );
  };

  const renderResult = (resp, student) => {
    const scores = resp.teacher_dimension_scores || resp.ai_dimension_scores || {};
    const isReviewed = resp.teacher_reviewed;
    const rating = resp.teacher_rating;
    const ratingMeta = RATING_META[rating];
    const dims = Object.entries(scores).slice(0, 5);
    return (
      <div className="mt-3 rounded-xl border border-slate-200 bg-white p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-bold rounded-full px-2.5 py-1 bg-green-600 text-white">已完成</span>
          {ratingMeta && (
            <span className={`text-xs font-bold rounded-full px-2.5 py-1 ${ratingMeta.cls}`}>{ratingMeta.label}</span>
          )}
          {isReviewed && (
            <span className="text-xs font-bold rounded-full px-2.5 py-1 bg-indigo-600 text-white">已确认</span>
          )}
        </div>
        {resp.teacher_note && (
          <div className="mt-1.5 text-xs text-slate-500 truncate">{resp.teacher_note}</div>
        )}
        <div className="flex flex-wrap gap-1 mt-2">
          {dims.map(([dim, r]) => (
            <span key={dim} className="text-[10px] bg-slate-100 text-slate-600 rounded px-1.5 py-0.5">
              {dim} · {r}
            </span>
          ))}
        </div>
        <div className="mt-1.5">
          <button
            onClick={() => setExpanded(prev => ({ ...prev, [resp.id]: !prev[resp.id] }))}
            className="text-[10px] text-indigo-500 underline"
          >
            {expanded[resp.id] ? '收起维度详情' : '展开维度详情'}
          </button>
          {expanded[resp.id] && (
            <div className="mt-1.5 text-[10px] text-slate-500 space-y-1">
              {dims.map(([dim, r]) => (
                <div key={dim}>{dim}：{r} — {resp.ai_reasoning?.[dim]?.reasoning || '（无推理说明）'}</div>
              ))}
              {resp.cleaned_text && <div>清洗稿：{resp.cleaned_text.slice(0, 80)}…</div>}
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderQuickRating = (resp) => {
    const current = resp.teacher_rating || '';
    return (
      <div className="mt-3 rounded-xl border border-slate-200 bg-white p-3">
        <div className="text-xs font-semibold text-slate-600 mb-1.5">
          当场判断（可选，推给 AI 评估前记录）
        </div>
        <div className="flex gap-1.5">
          {Object.entries(RATING_META).map(([key, meta]) => (
            <button
              key={key}
              onClick={() => handleQuickRate(resp, current === key ? '' : key)}
              disabled={liveBusy[resp.id]}
              className={`flex-1 text-[11px] font-bold rounded-lg py-2 transition ${
                current === key
                  ? meta.cls
                  : 'bg-white border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40'
              }`}
            >
              {meta.label}
            </button>
          ))}
        </div>
        <textarea
          value={noteDrafts[resp.id] || ''}
          onChange={e => setNoteDrafts(prev => ({ ...prev, [resp.id]: e.target.value }))}
          rows={1}
          placeholder="轻批注（可选，随当场判断一并保存）"
          className="mt-2 w-full text-[11px] border border-slate-200 rounded-lg p-1.5 outline-none focus:ring-1 focus:ring-indigo-300"
        />
        {current && (
          <div className="mt-1.5 text-[10px] text-green-600">
            已记录：{RATING_META[current].label}（点击可取消）
          </div>
        )}
      </div>
    );
  };

  const renderDialogue = (resp) => {
    const turns = liveDialogue[resp.id] || [];
    const roleLabel = { student: '学生', ai_suggestion: 'AI 追问', teacher: '教师追问' };
    if (turns.length === 0) {
      const text = (resp.raw_text || resp.cleaned_text || '').trim();
      if (text) {
        return <div className="text-sm text-slate-600 leading-relaxed whitespace-pre-wrap">{text}</div>;
      }
      return <div className="text-xs text-slate-400">暂无对话记录。</div>;
    }
    return (
      <div className="space-y-2.5">
        {turns.map((t, i) => (
          <div
            key={i}
            className={`rounded-lg p-2.5 border ${
              t.role === 'student'
                ? 'bg-blue-50 border-blue-100'
                : t.role === 'teacher'
                  ? 'bg-indigo-50 border-indigo-100'
                  : 'bg-slate-50 border-slate-100'
            }`}
          >
            <div className="text-[10px] font-semibold text-slate-400 mb-1">
              {roleLabel[t.role] || t.role}{t.turn_type === 'echo_risk' ? '（疑似复述）' : ''}
            </div>
            <div className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap">{t.content}</div>
          </div>
        ))}
      </div>
    );
  };

  const renderCard = (student) => {
    const resp = respFor(student.id);
    const status = statusOf(student.id);
    const meta = STATUS_META[status] || STATUS_META.not_started;
    const signals = studentSignals(student);
    const phase = resp ? liveTurnPhase[resp.id] : null;
    const phaseMeta = {
      awaiting_teacher: { label: '待老师发回', cls: 'bg-amber-100 text-amber-700' },
      awaiting_student: { label: '等学生发言', cls: 'bg-blue-100 text-blue-600' },
      ai_processing: { label: 'AI 处理中', cls: 'bg-slate-100 text-slate-500' },
      done: { label: '对话已结束', cls: 'bg-slate-100 text-slate-500' },
    }[phase];
    const accent = {
      recording: 'border-t-red-400', submitted: 'border-t-blue-400',
      processing: 'border-t-amber-400', processed: 'border-t-green-500',
      not_started: 'border-t-slate-200',
    }[status] || 'border-t-slate-200';
    const avatarCls = {
      recording: 'bg-red-100 text-red-600', submitted: 'bg-blue-100 text-blue-600',
      processing: 'bg-amber-100 text-amber-600', processed: 'bg-green-100 text-green-700',
      not_started: 'bg-slate-100 text-slate-400',
    }[status] || 'bg-slate-100 text-slate-400';
    const focused = focusedStudent?.id === student.id;
    const needsAttention = signals.length > 0;
    return (
      <div
        key={student.id}
        onClick={() => setFocusId(student.id)}
        className={`rounded-2xl border-t-4 border p-3.5 shadow-sm cursor-pointer transition hover:border-indigo-300 ${accent} ${focused ? 'ring-2 ring-indigo-300' : ''} ${needsAttention ? 'border-red-300 bg-red-50/40' : 'border-slate-200 bg-white'}`}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold shrink-0 ${avatarCls}`}>
              {student.name.slice(0, 1)}
            </span>
            <div>
              <div className="font-semibold text-slate-800 text-sm">{student.name}</div>
              <div className="text-[10px] text-slate-400">{student.grade}年级</div>
            </div>
          </div>
          <span className={`text-[10px] font-medium rounded-full px-2 py-0.5 shrink-0 ${phaseMeta ? phaseMeta.cls : meta.cls}`}>
            {phaseMeta ? phaseMeta.label : meta.label}
          </span>
        </div>
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[10px] text-slate-400">
            {status === 'not_started' ? '等待发言' : `${studentTurnCount(resp?.id) || 1} 轮`}
          </span>
          {needsAttention && <span className="text-[10px] font-bold text-red-600 animate-pulse">需处理</span>}
        </div>
        {signals.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {signals.slice(0, 3).map(s => (
              <span key={s.key} className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
            ))}
          </div>
        )}
      </div>
    );
  };

  if (!course || students.length === 0) {
    return (
      <div className="text-center text-slate-400 py-16">
        暂无班级数据，请先在「工作台 → 管理」中创建班级和学生。
      </div>
    );
  }

  const finishedCount = students.filter(s => {
    const r = respFor(s.id);
    return r && (liveFinished[r.id] || r.dialogue_finished);
  }).length;
  const pendingCount = students.filter(s => studentSignals(s).some(x => x.key === 'pending')).length;
  const sortedStudents = [...students].sort((a, b) => studentPriority(a) - studentPriority(b));
  const focusedStudent = students.find(s => s.id === focusId) || sortedStudents[0] || null;
  const focusedResp = focusedStudent ? respFor(focusedStudent.id) : null;
  const topicIdx = topics.findIndex(t => t.id === topic?.id);
  // 处理队列：需要老师出手的学生（待评估/复述风险/3轮已满/处理中），
  // 按优先级排序；教师点「已处理」移出，状态再次变化时自动重新入队。
  const queueItems = sortedStudents
    .map(s => {
      const resp = respFor(s.id);
      const signals = resp ? studentSignals(s) : [];
      const phase = resp ? liveTurnPhase[resp.id] : null;
      // “待发回”只在处理队列里出现（卡片右上角气泡已展示，避免重复）。
      if (
        resp && phase === 'awaiting_teacher' && statusOf(s.id) !== 'processed'
        && !(liveFinished[resp.id] || resp.dialogue_finished)
      ) {
        signals.push({ key: 'to_send', label: '待发回', cls: 'bg-amber-100 text-amber-700', score: 0.5 });
      }
      return { student: s, resp, signals };
    })
    .filter(item => item.signals.length > 0)
    .sort((a, b) => {
      const sa = Math.min(...a.signals.map(x => x.score));
      const sb = Math.min(...b.signals.map(x => x.score));
      return sa - sb;
    });

  return (
    <div className="flex flex-col xl:flex-row gap-4 items-start">
      <div className="flex-1 min-w-0 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-lg font-bold text-slate-800">{course?.class_name} · {course?.title}</div>
            <div className="text-xs text-slate-400 mt-0.5">课堂模式 · 合格线：1-3年级 ≥2.5，4-6年级及以上 ≥3.0（按学生年级判断达标）</div>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-[11px] text-slate-500 whitespace-nowrap">
              {topicIdx >= 0 ? `环节 ${topicIdx + 1}/${topics.length}` : ''}
            </span>
            <select
              value={topic?.id || ''}
              onChange={e => store.setLiveTopic(parseInt(e.target.value, 10))}
              className="border border-slate-200 rounded-lg px-2 py-1.5 outline-none bg-white text-xs max-w-[180px]"
            >
              {topics.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}
            </select>
            <button
              onClick={store.advanceLiveTopic}
              disabled={topicIdx >= topics.length - 1}
              className="text-[11px] px-3 py-1.5 rounded-lg bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-default cursor-pointer"
            >
              下一环节 →
            </button>
            <button
              onClick={handleClearSpeech}
              title="调试用：清空全班所有发言，重新开始"
              className="text-[11px] px-3 py-1.5 rounded-lg border border-red-200 text-red-600 bg-white hover:bg-red-50 cursor-pointer"
            >
              🧹 清除发言
            </button>
          </div>
        </div>

        {/* 课堂统计卡 */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {[
            { label: '已发言', value: `${spokenCount}/${students.length}`, cls: 'text-blue-600', dot: 'bg-blue-500' },
            { label: '已结束对话', value: `${finishedCount}/${students.length}`, cls: 'text-purple-600', dot: 'bg-purple-500' },
            { label: '已完成', value: `${doneCount}/${students.length}`, cls: 'text-green-600', dot: 'bg-green-500' },
            { label: '达标', value: `${passCount}/${doneCount}`, cls: 'text-emerald-600', dot: 'bg-emerald-500' },
            { label: '待评估', value: `${pendingCount}`, cls: pendingCount > 0 ? 'text-red-600' : 'text-slate-400', dot: pendingCount > 0 ? 'bg-red-500' : 'bg-slate-300' },
          ].map(stat => (
            <div key={stat.label} className="rounded-xl bg-white border border-slate-200 px-3 py-2 flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full shrink-0 ${stat.dot}`} />
              <div>
                <div className="text-[10px] text-slate-400 leading-none">{stat.label}</div>
                <div className={`text-sm font-bold mt-0.5 ${stat.cls}`}>{stat.value}</div>
              </div>
            </div>
          ))}
          <div className="rounded-xl bg-white border border-slate-200 px-3 py-2">
            <div className="text-[10px] text-slate-400 leading-none">评估进度</div>
            <div className="mt-1.5 h-1.5 bg-slate-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-500 rounded-full transition-all"
                style={{ width: `${students.length ? Math.round((doneCount / students.length) * 100) : 0}%` }}
              />
            </div>
          </div>
        </div>

        {focusedStudent && (
          <div className="rounded-2xl border-2 border-indigo-200 bg-white p-5 shadow-sm">
            <div className="flex items-start justify-between gap-3 flex-wrap">
              <div className="flex items-center gap-3">
                <span className="w-11 h-11 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center text-lg font-bold shrink-0">
                  {focusedStudent.name.slice(0, 1)}
                </span>
                <div>
                  <div className="text-lg font-bold text-slate-800">
                    {focusedStudent.name}
                    <span className="ml-1 text-xs font-normal text-slate-400">{focusedStudent.grade}年级</span>
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {statusOf(focusedStudent.id) === 'not_started'
                      ? '尚未发言'
                      : `${studentTurnCount(focusedResp?.id) || 1} 轮 · ${
                          {
                            awaiting_teacher: '待老师发回',
                            awaiting_student: '等学生发言',
                            ai_processing: 'AI 处理中',
                            done: '对话已结束',
                          }[focusedResp ? liveTurnPhase[focusedResp.id] : null] ||
                          STATUS_META[statusOf(focusedStudent.id)]?.label ||
                          ''
                        }`}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                {studentSignals(focusedStudent).map(s => (
                  <span key={s.key} className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
                ))}
                <button
                  onClick={() => openStudent(focusedStudent.id)}
                  className="text-[10px] text-slate-400 underline cursor-pointer"
                >
                  调试：学生窗口
                </button>
              </div>
            </div>

            <div className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div>
                <div className="text-xs font-semibold text-slate-500 mb-2">作答与对话上下文</div>
                <div className="max-h-[420px] overflow-y-auto pr-1">
                  {!focusedResp ? (
                    <div className="text-sm text-slate-400 py-10 text-center">该学生尚未发言</div>
                  ) : (
                    <>
                      {liveTranscripts[focusedResp.id] && statusOf(focusedStudent.id) !== 'processed' && (
                        <div className="mb-2 rounded-lg bg-red-50 border border-red-100 p-2">
                          <div className="text-[10px] text-red-400 mb-0.5">正在说 / 已说：</div>
                          <div className="text-xs text-red-700 leading-relaxed whitespace-pre-wrap">{liveTranscripts[focusedResp.id]}</div>
                        </div>
                      )}
                      {renderDialogue(focusedResp)}
                    </>
                  )}
                </div>
              </div>
              <div>
                <div className="text-xs font-semibold text-slate-500 mb-2">操作与评估</div>
                {focusedResp && (
                  <>
                    {liveMode === 'confirm' && (livePendingSuggestions[focusedResp.id]?.questions?.length > 0) && (
                      <div className="mb-2 space-y-2">
                        {(livePendingSuggestions[focusedResp.id].questions || []).map((q, idx) => (
                          <div key={`${focusedResp.id}-${idx}`} className="rounded-lg border border-indigo-200 bg-indigo-50 p-2">
                            <div className="text-[10px] text-indigo-400 mb-0.5">待发送追问</div>
                            <div className="text-xs text-slate-700 leading-relaxed">{q}</div>
                            <div className="flex gap-1.5 mt-1.5">
                              <button
                                onClick={() => store.sendAiSuggestion(focusedResp.id, q)}
                                className="flex-1 text-[10px] font-medium bg-indigo-600 text-white rounded-md py-1 cursor-pointer"
                              >
                                发送
                              </button>
                              <button
                                onClick={() => store.ignoreSuggestion(focusedResp.id)}
                                className="flex-1 text-[10px] text-slate-500 border border-slate-200 rounded-md py-1 cursor-pointer"
                              >
                                忽略
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    {liveAiQuestions[focusedResp.id] && !(liveFinished[focusedResp.id] || focusedResp.dialogue_finished) && (
                      <div className="mb-2 rounded-lg bg-indigo-50 border border-indigo-100 p-2">
                        <div className="text-[10px] text-indigo-400 mb-0.5">🤖 AI 已追问</div>
                        <div className="text-xs text-indigo-700 leading-relaxed">{liveAiQuestions[focusedResp.id]}</div>
                      </div>
                    )}
                    {statusOf(focusedStudent.id) === 'submitted' && (
                      <>
                        {renderTeacherAsk(focusedResp)}
                        {renderQuickRating(focusedResp)}
                      </>
                    )}
                    {statusOf(focusedStudent.id) === 'processing' && (
                      <div className="py-6 text-center text-sm text-amber-500">⏳ AI 评估处理中…</div>
                    )}
                    {statusOf(focusedStudent.id) === 'processed' && renderResult(focusedResp, focusedStudent)}
                    {!liveStatus[focusedResp.id] && statusOf(focusedStudent.id) !== 'processed' && (
                      <div className="mt-2 text-[10px] text-slate-400">（历史作答：仅展示，可直接评估）</div>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {sortedStudents.map(renderCard)}
        </div>
      </div>

      {/* ── AI 伴学控制台 ── */}
      <aside className="w-full xl:w-80 shrink-0 bg-white rounded-2xl border border-slate-200 shadow-sm p-4 space-y-4">
        {/* 处理队列 */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-semibold text-slate-700">处理队列</div>
            {queueItems.length > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-100 text-red-600 font-medium">
                {queueItems.length} 人待处理
              </span>
            )}
          </div>
          {queueItems.length === 0 ? (
            <div className="text-[11px] text-slate-300">队列为空，学生发言或 AI 评估完成会自动进入</div>
          ) : (
            <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
              {queueItems.map(({ student, resp, signals }) => {
                return (
                  <div key={student.id} className="rounded-lg border border-red-100 bg-red-50/40 p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] font-medium text-slate-700">
                        {student.name} <span className="text-slate-400">{student.grade}年级</span>
                      </span>
                      <div className="flex gap-1 shrink-0">
                        <button
                          onClick={() => setFocusId(student.id)}
                          className="text-[10px] px-1.5 py-0.5 rounded border border-indigo-200 text-indigo-600 bg-white cursor-pointer hover:bg-indigo-50"
                        >
                          聚焦
                        </button>
                      </div>
                    </div>
                    <div className="flex gap-1 flex-wrap mt-1">
                      {signals.map(sig => (
                        <span key={sig.key} className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${sig.cls}`}>
                          {sig.label}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="text-sm font-semibold text-slate-700">AI 伴学控制台</div>

        <div className="flex items-center gap-2">
          <div className="flex-1 flex rounded-lg border border-slate-200 overflow-hidden">
            <button
              onClick={() => store.setLiveMode('auto')}
              className={`flex-1 text-[11px] py-1.5 font-medium cursor-pointer transition-colors ${liveMode === 'auto' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-50'}`}
            >
              AI 自动
            </button>
            <button
              onClick={() => store.setLiveMode('confirm')}
              className={`flex-1 text-[11px] py-1.5 font-medium cursor-pointer transition-colors ${liveMode === 'confirm' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-50'}`}
            >
              老师确认
            </button>
          </div>
          <button
            onClick={store.togglePause}
            className={`text-[11px] px-3 py-1.5 rounded-lg border font-medium cursor-pointer ${livePaused ? 'bg-green-50 text-green-700 border-green-300' : 'bg-slate-50 text-slate-600 border-slate-200'}`}
          >
            {livePaused ? '▶ 继续' : '⏸ 暂停'}
          </button>
        </div>

      </aside>
    </div>
  );
}
