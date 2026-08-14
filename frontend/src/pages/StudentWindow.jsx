import { useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { subscribeStatus, publishStatus } from '../utils/statusBus';
import { getMode, resolveMode, subscribeModeChange } from '../config/mode';

// Demo fallback transcript used when the environment cannot run real ASR.
const DEMO_TRANSCRIPT =
  '我觉得应该把老鹰放回野外。因为老鹰本来就是天空的动物，关在动物园里就只能走来走去，很不自由。';

const MAX_ROUNDS = 3;

const STATUS_TEXT = {
  not_started: '未开始', recording: '正在发言', submitted: '已提交',
  processing: '处理中', processed: '已处理',
};

const BUBBLE_CLS = {
  ai: 'bg-indigo-50 border border-indigo-100 text-indigo-900',
  student: 'bg-blue-500 text-white',
  teacher: 'bg-amber-50 border border-amber-200 text-amber-900',
};

export default function StudentWindow({ studentId }) {
  const [isDemo, setIsDemo] = useState(getMode() !== 'real');
  const [courseId, setCourseId] = useState(null);
  const [topicId, setTopicId] = useState(null);
  const [student, setStudent] = useState(null);
  const [topic, setTopic] = useState(null);
  const [responseId, setResponseId] = useState(null);
  const [status, setStatus] = useState('not_started');
  const [turnCount, setTurnCount] = useState(0);
  const [messages, setMessages] = useState([]); // {role:'ai'|'student'|'teacher', content}
  const [lastTeacherQuestion, setLastTeacherQuestion] = useState('');
  const [transcript, setTranscript] = useState('');
  const [pasteText, setPasteText] = useState('');
  const [recording, setRecording] = useState(false);
  const [simCountdown, setSimCountdown] = useState(0);
  const [simNote, setSimNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [aiThinking, setAiThinking] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [feedbackLoading, setFeedbackLoading] = useState(false);
  const [showStimulus, setShowStimulus] = useState(true);

  useEffect(() => {
    // 模式事实源：自动探测 + 跟随教师端切换（跨窗口经 storage 事件同步）。
    resolveMode().then(m => setIsDemo(m === 'demo'));
    return subscribeModeChange(() => setIsDemo(getMode() !== 'real'));
  }, []);
  const [ended, setEnded] = useState(false);
  const [encouragement, setEncouragement] = useState('');

  const mediaRecorderRef = useRef(null);
  const streamRef = useRef(null);
  const chunksRef = useRef([]);
  const dialogueRef = useRef(null);
  const responseIdRef = useRef(null);
  const endedRef = useRef(false);
  const modeRef = useRef('auto');   // 'auto' | 'confirm'（来自教师端广播）
  const pausedRef = useRef(false);  // 全局暂停 AI 伴学
  responseIdRef.current = responseId;
  endedRef.current = ended;

  // 挂载时读取教师端当前模式/暂停（独立窗口可能错过了切换广播，HMR 重挂载也靠这里恢复）
  useEffect(() => {
    try {
      if (typeof localStorage !== 'undefined') {
        modeRef.current = localStorage.getItem('weixue-live-mode') === 'confirm' ? 'confirm' : 'auto';
        pausedRef.current = localStorage.getItem('weixue-live-paused') === '1';
      }
    } catch { /* ignore */ }
  }, []);

  const dialogueToMessages = (dialogue) =>
    (dialogue || []).map(t => ({
      role: t.role === 'student' ? 'student' : t.role === 'teacher' ? 'teacher' : 'ai',
      content: t.content,
    }));

  // ── Bootstrap: locate student + topic ───────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // The query lives AFTER the # (e.g. .../index.html#/student/3?course=1&topic=2),
        // so window.location.search is empty — parse it out of the hash.
        const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
        const courses = await api.getCourses();
        const paramCid = Number(params.get('course')) || null;
        const scopedCourses = paramCid ? courses.filter(c => c.id === paramCid) : courses;
        const allStudents = [];
        for (const c of scopedCourses) {
          const list = await api.getStudents(c.id);
          list.forEach(s => allStudents.push({ ...s, course_id: c.id }));
        }
        const me = allStudents.find(s => s.id === Number(studentId));
        if (!me) return;
        const cid = Number(me.course_id);
        const topics = await api.getTopics(cid);
        const tid = Number(params.get('topic')) || topics[0]?.id || null;
        const t = topics.find(x => x.id === tid) || topics[0] || null;
        if (cancelled) return;
        setCourseId(cid);
        setTopicId(t ? t.id : null);
        setStudent(me);
        setTopic(t || null);

        // Resume an existing response for this student+topic.
        const resps = await api.getResponses(cid, me.id);
        const mine = resps.find(r => r.topic_id === t?.id);
        if (mine) {
          responseIdRef.current = mine.id;
          setResponseId(mine.id);
          setStatus(mine.processing_status && mine.processing_status !== 'not_started'
            ? mine.processing_status : 'not_started');
          const dialogue = await api.getDialogue(mine.id);
          setMessages(dialogueToMessages(dialogue));
          const studentTurns = dialogue.filter(x => x.role === 'student');
          setTurnCount(studentTurns.length);
          // 刷新后恢复对话生命周期：已结束的对话保持完成态
          if (mine.dialogue_finished) {
            endedRef.current = true;
            setEnded(true);
          }
        }
      } catch (e) {
        console.error('StudentWindow bootstrap failed:', e);
      }
    })();
    return () => { cancelled = true; };
  }, [studentId]);

  // ── Mirror status from the live bus (teacher/other windows) ──
  useEffect(() => {
    if (!courseId) return;
    const unsubscribe = subscribeStatus(courseId, (evt) => {
      if (evt.type === 'teacher_question' && evt.responseId === responseIdRef.current) {
        setLastTeacherQuestion(evt.question || '');
        setMessages(prev => [...prev, { role: 'teacher', content: evt.question || '' }]);
        return;
      }
      if (evt.type === 'teacher_finished' && evt.responseId === responseIdRef.current) {
        setEnded(true);
        setSimNote('（老师结束了这轮对话）');
        return;
      }
      if (evt.type === 'live_mode') {
        modeRef.current = evt.mode === 'confirm' ? 'confirm' : 'auto';
        return;
      }
      if (evt.type === 'live_pause') {
        pausedRef.current = !!evt.paused;
        return;
      }
      if (evt.type === 'ai_question' && evt.responseId === responseIdRef.current) {
        setMessages(prev => (
          prev[prev.length - 1]?.content === evt.question
            ? prev
            : [...prev, { role: 'ai', content: evt.question }]
        ));
        return;
      }
      if (evt.responseId === responseIdRef.current && evt.status) {
        setStatus(evt.status);
      }
    });
    return unsubscribe;
  }, [courseId]);

  // ── Poll dialogue so turns / teacher interventions stay fresh ──
  useEffect(() => {
    if (!courseId || !responseId) return;
    const timer = setInterval(async () => {
      try {
        const dialogue = await api.getDialogue(responseId);
        setMessages(dialogueToMessages(dialogue));
        const studentTurns = dialogue.filter(x => x.role === 'student');
        setTurnCount(studentTurns.length);
      } catch { /* ignore polling errors */ }
    }, 3000);
    return () => clearInterval(timer);
  }, [courseId, responseId]);

  // ── 完成态：拉取 LLM 闪光点反馈（带兜底）──
  useEffect(() => {
    if ((turnCount >= MAX_ROUNDS || endedRef.current) && responseId && feedback === null && !feedbackLoading) {
      setFeedbackLoading(true);
      api.flashFeedback(responseId)
        .then(r => setFeedback(r?.feedback || '你把自己的想法说出来啦，真棒！'))
        .catch(() => setFeedback('你把自己的想法说出来啦，真棒！'))
        .finally(() => setFeedbackLoading(false));
    }
  }, [turnCount, responseId, feedback, feedbackLoading]);

  // 对话自动滚动到底部
  useEffect(() => {
    const el = dialogueRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [messages, aiThinking]);

  const publish = (s, response, round, extra = {}) => {
    setStatus(s);
    publishStatus(courseId, {
      responseId: responseIdRef.current,
      status: s,
      studentId: Number(studentId),
      response: response || null,
      round: round || undefined,
      transcript: transcript || undefined,
      ...extra,
    });
  };

  // ── AI 自动追问（企业要求：AI 直接和学生对话）──
  const autoAsk = async (rid) => {
    if (endedRef.current) return; // 对话已结束则不再生成
    setAiThinking(true);
    try {
      const suggestion = await api.suggestTurn(rid);
      if (endedRef.current) return; // await 期间被结束则丢弃结果
      if (pausedRef.current) return; // 全局暂停期间不继续
      if (modeRef.current === 'confirm') {
        // 确认模式：建议只进教师端控制台“待发送”，不直接推给学生
        publishStatus(courseId, {
          responseId: rid,
          studentId: Number(studentId),
          type: 'ai_suggestion_ready',
          questions: suggestion?.questions || [],
          echoRisk: suggestion?.scaffold_status === 'echo_risk',
          note: suggestion?.note || '',
        });
        setSimNote('（AI 已准备好下一问，等老师确认发送）');
        return;
      }
      const q = suggestion?.questions?.[0];
      if (q) {
        await api.appendTurn(rid, { role: 'ai_suggestion', content: q, turn_type: 'scaffold' });
        setMessages(prev => [...prev, { role: 'ai', content: q }]);
        publishStatus(courseId, {
          responseId: rid, studentId: Number(studentId), type: 'ai_question', question: q,
          echoRisk: suggestion.scaffold_status === 'echo_risk',
        });
        if (suggestion.scaffold_status === 'echo_risk') {
          setSimNote('（AI 注意到你可能在重复，换了一种开放问法）');
        }
      }
    } catch (e) {
      console.warn('autoAsk failed:', e);
      // AI 暂时无响应（超时/后端不可用）也不能让学生干等：发一条备用追问，
      // 对话继续，教师端仍能看到并接管。
      const fallback = '你的理由和结论之间是不是缺了什么？要不要补充一下？';
      try {
        await api.appendTurn(rid, { role: 'ai_suggestion', content: fallback, turn_type: 'scaffold' });
      } catch { /* 备用追问持久化失败也继续 */ }
      setMessages(prev => [...prev, { role: 'ai', content: fallback }]);
      publishStatus(courseId, {
        responseId: rid,
        studentId: Number(studentId),
        type: 'ai_question',
        question: fallback,
        echoRisk: false,
      });
      setSimNote('（AI 暂时没响应，已用备用追问）');
    } finally {
      setAiThinking(false);
    }
  };

  const submitTranscript = async (text) => {
    if (!text.trim() || !courseId || !topicId) return null;
    setBusy(true);
    try {
      let updated;
      if (responseIdRef.current) {
        updated = await api.appendTurn(responseIdRef.current, { role: 'student', content: text.trim(), turn_type: '' });
      } else {
        updated = await api.importText(courseId, Number(studentId), topicId, text.trim(), 'student_device');
      }
      responseIdRef.current = updated.id;
      setResponseId(updated.id);
      const newCount = (turnCount || 0) + 1;
      setTurnCount(newCount);
      setMessages(prev => [...prev, { role: 'student', content: text.trim() }]);
      setTranscript('');
      setPasteText('');
      publish('submitted', updated, newCount, { transcript: text.trim() });
      // 每轮作答后给一句即时鼓励（轻量规则，不调 LLM）
      if (/因为|所以|如果/.test(text)) {
        setEncouragement('你说出了想法，还用理由撑住了它！');
      } else if (newCount === 1) {
        setEncouragement('你把想法说出来啦，真棒！');
      } else if (newCount === 2) {
        setEncouragement('你又补充了新角度！');
      } else {
        setEncouragement('你坚持说了这么多轮，很有耐心！');
      }
      if (!endedRef.current && newCount < MAX_ROUNDS) {
        await autoAsk(updated.id);
      } else {
        setSimNote(newCount >= MAX_ROUNDS ? '（已完成 3 轮对话，等待老师评估）' : '（对话已结束，等待老师评估）');
      }
      return updated.id;
    } catch (e) {
      console.error('submit failed:', e);
      return null;
    } finally {
      setBusy(false);
    }
  };

  // ── Recording ───────────────────────────────────────────
  const startRecording = async () => {
    if (recording || busy || aiThinking) return;
    const canRecord = typeof navigator !== 'undefined'
      && navigator.mediaDevices?.getUserMedia
      && typeof MediaRecorder !== 'undefined';

    if (!canRecord) {
      // Simulated recording fallback (demo-friendly), streaming the transcript
      // to the teacher cockpit so "正在说…" is visible in real time.
      setRecording(true);
      setSimNote('（模拟录音中…）');
      publish('recording');
      for (let i = 3; i >= 0; i--) {
        setSimCountdown(i);
        await new Promise(r => setTimeout(r, 500));
      }
      setSimCountdown(0);
      if (responseIdRef.current) {
        for (let i = 1; i <= DEMO_TRANSCRIPT.length; i += 8) {
          const partial = DEMO_TRANSCRIPT.slice(0, i);
          setTranscript(partial);
          publishStatus(courseId, {
            responseId: responseIdRef.current,
            studentId: Number(studentId),
            transcript: partial,
          });
          await new Promise(r => setTimeout(r, 160));
        }
      } else {
        await new Promise(r => setTimeout(r, 600));
      }
      setRecording(false);
      setSimNote(isDemo ? '（演示环境：使用模拟转写文本）' : '（未获得麦克风权限，请改用粘贴文本）');
      await submitTranscript(DEMO_TRANSCRIPT);
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        setRecording(false);
        if (isDemo) {
          setSimNote('（演示环境：录音已采集，转写内容为模拟）');
          await submitTranscript(DEMO_TRANSCRIPT);
        } else {
          setSimNote('（正在转写语音…）');
          try {
            const resp = await api.importAudio(courseId, Number(studentId), topicId, blob, 'student_device');
            responseIdRef.current = resp.id;
            setResponseId(resp.id);
            const newCount = (turnCount || 0) + 1;
            setTurnCount(newCount);
            setMessages(prev => [...prev, { role: 'student', content: resp.raw_text || '' }]);
            publish('submitted', resp, newCount, { transcript: resp.raw_text || '' });
            setEncouragement(newCount === 1 ? '你把想法说出来啦，真棒！' : '你又补充了新想法！');
            if (!endedRef.current && newCount < MAX_ROUNDS) await autoAsk(resp.id);
            else setSimNote(newCount >= MAX_ROUNDS ? '（已完成 3 轮对话，等待老师评估）' : '（对话已结束，等待老师评估）');
          } catch (e) {
            console.error('audio import failed:', e);
            setSimNote('（语音转写失败，请改用粘贴文本）');
          }
        }
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
      setSimNote('（正在录音…请口述你的回答）');
      publish('recording');
    } catch (e) {
      console.warn('mic denied:', e);
      setSimNote('（麦克风权限被拒，请改用粘贴文本）');
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop();
    }
  };

  const handleStop = () => {
    if (recording) stopRecording();
  };

  const finishNow = () => {
    if (endedRef.current || recording || turnCount === 0 || !responseIdRef.current) return;
    endedRef.current = true;
    setEnded(true);
    setAiThinking(false);
    setSimNote('（对话已结束，等待老师评估）');
    publishStatus(courseId, {
      responseId: responseIdRef.current,
      studentId: Number(studentId),
      type: 'student_finished',
    });
    api.finishDialogue(responseIdRef.current, 'student').catch(() => {});
  };

  const done = turnCount >= MAX_ROUNDS || ended;
  const roundText = done
    ? (turnCount >= MAX_ROUNDS ? '（已达 3 轮上限）' : '（已结束）')
    : `第 ${turnCount + 1} 轮`;

  return (
    <div className="min-h-screen bg-gradient-to-b from-indigo-50 to-white">
      <div className="max-w-md mx-auto min-h-screen flex flex-col">
        {/* Header */}
        <div className="bg-indigo-600 text-white px-5 py-4">
          <div className="text-xs opacity-80">AI 伴学 · 随堂口述练习</div>
          <div className="flex items-center justify-between mt-0.5">
            <div className="text-xl font-bold">
              {student ? `${student.name}（${student.grade} 年级）` : '加载中…'}
            </div>
            <span className="rounded-full px-3 py-1 text-xs font-medium bg-white/20">
              {STATUS_TEXT[status] || status} · {roundText}
            </span>
          </div>
        </div>

        {/* Story / question */}
        <div className="px-4 pt-4">
          <div className="rounded-xl bg-slate-50 border border-slate-200 p-4">
            <div className="flex items-center justify-between">
              <div className="text-xs text-slate-400 mb-1">思辨题目</div>
              <button
                onClick={() => setShowStimulus(!showStimulus)}
                className="text-[10px] text-slate-400 underline"
              >
                {showStimulus ? '收起' : '展开'}
              </button>
            </div>
            <div className="text-sm font-semibold text-slate-800">{topic?.title || '加载题目中…'}</div>
            {showStimulus && topic?.stimulus_material && (
              <div className="text-sm text-slate-600 mt-2 leading-relaxed">{topic.stimulus_material}</div>
            )}
          </div>
        </div>

        {/* Dialogue flow */}
        <div ref={dialogueRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {messages.length === 0 && !aiThinking && (
            <div className="text-center text-xs text-slate-400 py-8">点击下方麦克风，把你的想法说出来 🎙️</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'student' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[82%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed shadow-sm ${BUBBLE_CLS[m.role] || BUBBLE_CLS.ai}`}>
                {m.role === 'ai' && <div className="text-[10px] text-indigo-400 mb-0.5">🤖 AI</div>}
                {m.role === 'teacher' && <div className="text-[10px] text-amber-500 mb-0.5">👩‍🏫 老师</div>}
                {m.content}
              </div>
            </div>
          ))}
          {encouragement && !recording && (
            <div className="text-center">
              <span className="inline-block text-[11px] text-green-600 bg-green-50 border border-green-100 rounded-full px-3 py-1">
                💬 {encouragement}
              </span>
            </div>
          )}
          {recording && transcript && (
            <div className="flex justify-end">
              <div className="max-w-[82%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed shadow-sm bg-blue-500 text-white">
                {transcript}<span className="animate-pulse">▍</span>
              </div>
            </div>
          )}
          {aiThinking && (
            <div className="flex justify-start">
              <div className="text-xs text-indigo-400 bg-indigo-50 border border-indigo-100 rounded-2xl px-3 py-2.5 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" />
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '0.15s' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '0.3s' }} />
              </div>
            </div>
          )}
        </div>

        {/* Bottom controls */}
        <div className="px-4 pb-6 pt-3 space-y-3 bg-white/80 backdrop-blur rounded-t-2xl border-t border-slate-100">
          {done ? (
            <div className="rounded-xl bg-green-50 border border-green-200 p-4 text-center">
              <div className="text-sm font-semibold text-green-700">
                🎉 {turnCount >= MAX_ROUNDS ? '3 轮对话完成！' : '对话完成！'}
              </div>
              {feedbackLoading ? (
                <div className="text-xs text-green-600 mt-2 animate-pulse">AI 正在整理你的反馈…</div>
              ) : (
                feedback && <div className="text-sm text-green-800 mt-2 leading-relaxed">{feedback}</div>
              )}
              <div className="text-[11px] text-green-600 mt-2">回答已交给老师，评估完成后会给你更完整的反馈。</div>
            </div>
          ) : (
            <>
              <div className="text-center">
                <div className="flex justify-center gap-1.5 mb-2">
                  {[1, 2, 3].map(n => (
                    <span key={n} className={`w-1.5 h-1.5 rounded-full ${turnCount >= n ? 'bg-indigo-500' : 'bg-slate-200'}`} />
                  ))}
                </div>
                {!recording ? (
                  <button
                    onClick={startRecording}
                    disabled={busy || aiThinking || status === 'processing'}
                    className="w-32 h-32 rounded-full bg-indigo-600 text-white text-base font-bold shadow-lg hover:bg-indigo-700 active:scale-95 transition-transform disabled:opacity-40"
                  >
                    <div className="text-4xl mb-1">🎙️</div>
                    开麦口述
                  </button>
                ) : (
                  <div className="flex flex-col items-center">
                    <button
                      onClick={handleStop}
                      className="w-32 h-32 rounded-full bg-red-500 text-white text-base font-bold shadow-lg animate-pulse hover:bg-red-600 active:scale-95 transition-transform"
                    >
                      <div className="text-4xl mb-1">⏹️</div>
                      停止
                    </button>
                    <div className="text-xs text-red-500 mt-2">
                      {simCountdown > 0 ? `${simCountdown}…` : '正在录音'}
                    </div>
                  </div>
                )}
                <div className="text-xs text-slate-400 mt-2">
                  {aiThinking ? 'AI 正在思考下一问…' : (simNote || '口述后 AI 会接着追问，最多 3 轮')}
                </div>
              </div>

              {turnCount > 0 && !done && (
                <button
                  onClick={finishNow}
                  disabled={recording}
                  className="w-full text-sm font-medium text-emerald-700 border border-emerald-300 bg-emerald-50 rounded-lg py-2 hover:bg-emerald-100 disabled:opacity-40"
                >
                  ✅ 完成作答，交给老师
                </button>
              )}

              {/* Paste fallback */}
              <div className="rounded-xl border border-slate-200 p-3">
                <textarea
                  value={pasteText}
                  onChange={e => setPasteText(e.target.value)}
                  rows={2}
                  placeholder="也可以在这里输入/粘贴你的回答（兜底）"
                  className="w-full text-sm border border-slate-200 rounded-lg p-2 outline-none focus:ring-1 focus:ring-indigo-300"
                />
                <button
                  onClick={() => submitTranscript(pasteText)}
                  disabled={busy || aiThinking || !pasteText.trim()}
                  className="mt-2 w-full text-sm font-medium bg-indigo-100 text-indigo-700 rounded-lg py-2 hover:bg-indigo-200 disabled:opacity-40"
                >
                  {busy ? '提交中…' : '提交回答'}
                </button>
              </div>
            </>
          )}
          <div className="text-[11px] text-slate-400 text-center">
            {isDemo
              ? '演示模式：AI 直接追问，教师端可实时看到你的发言与轮次'
              : '口述内容将由 AI 语音识别转为文字，AI 会接着向你提问'}
          </div>
        </div>
      </div>
    </div>
  );
}
