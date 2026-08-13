import { useState, useEffect, useRef, useCallback } from 'react';
import useStore from '../stores/gradingStore';
import * as api from '../api/client';
import { averageRating, bandForGrade, passLineForGrade, upgradeBand, collectBonusFlags } from '../utils/ratings';
const BAND_CLS = {
  优秀: 'text-green-600', 良好: 'text-emerald-600',
  待提升: 'text-yellow-600', 薄弱: 'text-red-600', 未评: 'text-slate-400',
};
const ratingLabel = (avg, bonusFlags, grade) => {
  const band = upgradeBand(bandForGrade(avg, grade), bonusFlags);
  return { text: band, cls: BAND_CLS[band] || BAND_CLS.未评, upgraded: Array.isArray(bonusFlags) && bonusFlags.length > 0 && band !== '未评' };
};

const DELIVERY_LABEL = {
  sending: { text: '飞书发送中', cls: 'text-amber-700 bg-amber-50 border-amber-200' },
  delivered: { text: '✓ 评语已送达', cls: 'text-green-700 bg-green-50 border-green-200' },
  failed: { text: '飞书发送失败', cls: 'text-red-600 bg-red-50 border-red-200' },
};

const DIM_LABELS = {
  position: '立意（观点鲜明）', material: '选材（言之有物）',
  structure: '结构（条理清晰）', language: '语言（用词准确）',
  perspective: '视角（换位思考）',
  // 旧数据兼容：老维度 key 也统一显示为五维度
  clarity: '立意（观点鲜明）', interpretation: '立意（观点鲜明）',
  evidence_awareness: '选材（言之有物）', evidence_use: '选材（言之有物）',
  relevance: '结构（条理清晰）', inference: '结构（条理清晰）',
  argument_evaluation: '结构（条理清晰）', depth_breadth: '视角（换位思考）',
  self_regulation: '视角（换位思考）',
  清晰性: '立意（观点鲜明）', 解释力: '立意（观点鲜明）',
  证据意识: '选材（言之有物）', 证据使用: '选材（言之有物）',
  相关性: '结构（条理清晰）', 因果推理: '结构（条理清晰）',
  论证质量: '结构（条理清晰）', 深度广度: '视角（换位思考）',
  反思调节: '视角（换位思考）',
};

export default function CommentsPage() {
  const { students, topics, responses, currentStudentIdx, setStudentIdx, courseId, loadCourse, refreshStudents } = useStore();
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(false);
  const [saveStatus, setSaveStatus] = useState(''); // '' | 'saving' | 'saved'
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchResult, setBatchResult] = useState(null);
  const [sendStatus, setSendStatus] = useState({ kind: '', message: '' });
  const saveTimer = useRef(null);
  const saveRequest = useRef(null);
  const pendingSave = useRef(null);

  const student = students[currentStudentIdx];

  // Load a draft when the selected student changes. Polling replaces student
  // objects but keeps the same ID, so it cannot overwrite an in-progress edit.
  useEffect(() => {
    if (student) {
      setDraft(student.comment_draft || '');
      setSaveStatus('');
      setSendStatus({ kind: '', message: '' });
    }
  }, [student?.id]);

  // The teacher confirms delivery in Feishu after leaving this browser action
  // idle. Keep the student records fresh so sending/delivered/failed appears on
  // this page without requiring a manual reload or a trip to student manager.
  useEffect(() => {
    if (!courseId) return undefined;

    let cancelled = false;
    let timer = null;
    const pollDeliveryStatus = async () => {
      try {
        await refreshStudents(courseId);
      } catch (error) {
        console.warn('刷新飞书投递状态失败:', error);
      } finally {
        if (!cancelled) timer = window.setTimeout(pollDeliveryStatus, 2000);
      }
    };

    timer = window.setTimeout(pollDeliveryStatus, 1000);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [courseId, refreshStudents]);

  const draftMatchesSaved = Boolean(
    student && draft === (student.comment_draft || '')
  );
  const currentDeliveryStatus = draftMatchesSaved && !loading && !batchLoading
    ? student.comment_delivery_status || 'not_sent'
    : 'not_sent';

  // Reflect the asynchronous student delivery result in the action area too,
  // so its button/message cannot contradict the status badge above.
  useEffect(() => {
    if (!student || !draftMatchesSaved) return;
    if (currentDeliveryStatus === 'delivered') {
      setSendStatus({ kind: 'delivered', message: '评语已成功送达该学生。' });
    } else if (currentDeliveryStatus === 'sending') {
      setSendStatus({ kind: 'delivery_sending', message: '正在通过飞书发送给学生…' });
    } else if (currentDeliveryStatus === 'failed') {
      setSendStatus({
        kind: 'delivery_failed',
        message: student.comment_delivery_error
          ? `发送失败：${student.comment_delivery_error}`
          : '发送失败，请重新发送确认卡后重试。',
      });
    } else {
      setSendStatus(current => (
        ['delivered', 'delivery_sending', 'delivery_failed'].includes(current.kind)
          ? { kind: '', message: '' }
          : current
      ));
    }
  }, [
    student?.id,
    student?.comment_delivery_status,
    student?.comment_delivery_error,
    draftMatchesSaved,
    currentDeliveryStatus,
  ]);

  const flushPendingSave = useCallback(async () => {
    // Serialize saves so an older, slower request cannot overwrite a newer
    // draft when the user keeps typing while the previous save is in flight.
    if (saveRequest.current) {
      try {
        await saveRequest.current;
      } catch {
        // Continue with the newest pending text after a transient failure.
      }
    }
    const pending = pendingSave.current;
    if (!pending) return;
    pendingSave.current = null;
    const request = api.saveCommentDraft(
      pending.courseId,
      pending.studentId,
      pending.text,
    );
    saveRequest.current = request;
    try {
      await request;
      setSaveStatus('saved');
      setTimeout(() => setSaveStatus(''), 2000);
    } catch {
      setSaveStatus('');
    } finally {
      if (saveRequest.current === request) saveRequest.current = null;
    }
  }, []);

  // Debounced auto-save. Keep the pending payload in a ref so actions such as
  // regeneration can flush it instead of silently discarding the edit.
  const autoSave = useCallback((text) => {
    if (!student || !courseId) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    setSaveStatus('saving');
    pendingSave.current = { courseId, studentId: student.id, text };
    saveTimer.current = setTimeout(() => {
      saveTimer.current = null;
      void flushPendingSave();
    }, 800);
  }, [student?.id, courseId, flushPendingSave]);

  useEffect(() => () => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    const pending = pendingSave.current;
    if (pending) {
      pendingSave.current = null;
      // Preserve a final edit when the user leaves the page. Avoid component
      // state updates here because the page is already unmounting. Chain it
      // after an in-flight save so an older request cannot finish last.
      const current = saveRequest.current;
      void (async () => {
        if (current) {
          try {
            await current;
          } catch {
            // Still persist the newest pending text.
          }
        }
        try {
          await api.saveCommentDraft(pending.courseId, pending.studentId, pending.text);
        } catch {
          // The page is gone, so there is no useful local status to update.
        }
      })();
    }
  }, []);

  const settleAutoSave = async () => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    // Another timer-triggered flush may take the pending payload while this
    // action is waiting. Recheck until neither a request nor payload remains.
    while (saveRequest.current || pendingSave.current) {
      await flushPendingSave();
    }
  };

  const handleDraftChange = (e) => {
    const text = e.target.value;
    setDraft(text);
    setSendStatus({ kind: '', message: '' });
    autoSave(text);
  };

  if (students.length === 0) return null;

  const resps = responses[student?.id] || [];
  const respMap = {};
  resps.forEach(r => { respMap[r.topic_id] = r; });

  // Per-topic teacher data
  let totalAvg = 0, topicCount = 0, reviewedCount = 0;
  const topicDetails = [];
  topics.forEach(t => {
    const r = respMap[t.id];
    if (!r || !r.raw_text || !r.raw_text.trim()) return;
    const scores = r.teacher_dimension_scores || r.ai_dimension_scores;
    const isReviewed = r.teacher_reviewed || false;
    if (isReviewed) reviewedCount++;
    if (scores) {
      const avg = averageRating(scores);
      totalAvg += avg;
      topicCount++;
      topicDetails.push({
        topic: t, avg, scores, isReviewed,
        tags: r.teacher_tags || [],
        note: r.teacher_note || '',
      });
    }
  });
  const studentAvg = topicCount > 0 ? totalAvg / topicCount : 0;
  const rl = ratingLabel(studentAvg, collectBonusFlags(resps), student.grade);
  const studentPassing = studentAvg > 0 && studentAvg >= passLineForGrade(student.grade);

  const generate = async () => {
    if (!student) return;
    // Regeneration represents a new final-comment candidate. Hide every
    // delivery indicator for the old draft immediately instead of waiting for
    // the request and the next status poll to complete.
    setSendStatus({ kind: '', message: '' });
    setSaveStatus('');
    setLoading(true);
    try {
      await settleAutoSave();
      const r = await api.generateComment(courseId, student.id);
      if (r?.draft) setDraft(r.draft);
      await loadCourse(courseId);
      setSaveStatus('');
    } catch (e) {
      console.error(e);
      setSendStatus({ kind: 'error', message: '重新生成失败，请稍后重试。' });
    }
    setLoading(false);
  };

  const batchGenerate = async () => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    const sid = student?.id;
    setBatchLoading(true);
    setBatchResult(null);
    setSendStatus({ kind: '', message: '' });
    try {
      await settleAutoSave();
      const r = await api.batchGenerateComments(courseId);
      setBatchResult(r);
      await loadCourse(courseId);
      // Prefer the batch result for the current student: direct and avoids
      // showing stale/empty data when loadCourse races or a student is skipped.
      const mine = (r.results || []).find(x => x.student_id === sid);
      if (mine && mine.draft) {
        setDraft(mine.draft);
      } else {
        const updated = useStore.getState().students[currentStudentIdx];
        if (updated) setDraft(updated.comment_draft || '');
      }
    } catch {
      setBatchResult({ results: [], error: '批量生成失败' });
    }
    setBatchLoading(false);
  };

  // Per-student info for selector
  const studentInfo = students.map((st, i) => {
    const ss = responses[st.id] || [];
    const sm = {};
    ss.forEach(r => { sm[r.topic_id] = r; });
    let avg = 0, cnt = 0, reviewed = 0;
    topics.forEach(t => {
      const r = sm[t.id];
      if (!r || !r.raw_text || !r.raw_text.trim()) return;
      if (r.teacher_reviewed) reviewed++;
      const scores = r.teacher_dimension_scores || r.ai_dimension_scores;
      if (scores) { avg += averageRating(scores); cnt++; }
    });
    const stAvg = cnt > 0 ? avg / cnt : 0;
    const passLine = passLineForGrade(st.grade);
    return {
      name: st.name, idx: i, grade: st.grade,
      avg: stAvg, reviewed, hasDraft: !!st.comment_draft,
      deliveryStatus: (
        batchLoading || (i === currentStudentIdx && draft !== (st.comment_draft || ''))
          ? 'not_sent'
          : st.comment_delivery_status || 'not_sent'
      ),
      bonus: collectBonusFlags(ss),
      passing: stAvg > 0 && stAvg >= passLine,
      passLine,
    };
  });

  const handleStudentChange = async (i) => {
    await settleAutoSave();
    setStudentIdx(i);
    const st = students[i];
    setDraft(st?.comment_draft || '');
    setSaveStatus('');
    setSendStatus({ kind: '', message: '' });
  };

  const send = async () => {
    if (!student || !draft.trim() || sendStatus.kind === 'sending') return;
    setSendStatus({ kind: 'sending', message: '正在保存评语并发送教师确认卡...' });
    try {
      await settleAutoSave();
      const result = await api.sendComment(courseId, student.id, draft.trim());
      if (result.status === 'delivered') {
        setSendStatus({
          kind: 'card_sent',
          message: '确认卡已发送给教师，请在飞书中确认后发送给学生。',
        });
      } else {
        setSendStatus({
          kind: 'pending',
          message: result.message || '评语已保存，但教师确认卡尚未发送，请稍后重试。',
        });
      }
      await loadCourse(courseId);
    } catch (error) {
      console.error(error);
      setSendStatus({
        kind: 'error',
        message: error?.response?.data?.detail || '保存或发送确认卡失败，请稍后重试。',
      });
    }
  };

  const sendButtonText = {
    sending: '发送中...',
    card_sent: '确认卡已发送给教师',
    delivery_sending: '正在发送给学生',
    delivered: '评语已送达',
    delivery_failed: '重新发送确认卡',
    pending: '重新发送确认卡',
    error: '重新发送确认卡',
  }[sendStatus.kind] || '发送给学生';

  return (
    <div className="flex flex-col gap-5">
      {/* ── Top bar: student selector + batch button ──────── */}
      <div className="bg-white rounded-xl p-3 border border-slate-200 flex items-center gap-2 flex-wrap">
        <span className="text-xs text-slate-500 shrink-0">选择学生：</span>
        <div className="flex gap-1.5 flex-wrap flex-1">
          {studentInfo.map((si, i) => {
            const siLabel = ratingLabel(si.avg, si.bonus, si.grade);
            return (
              <button key={i} onClick={() => handleStudentChange(i)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer
                  ${i === currentStudentIdx
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}`}>
                {si.name}
                {si.reviewed > 0 && (
                  <span className={`ml-1.5 text-[10px] px-1 rounded ${
                    i === currentStudentIdx
                      ? si.passing ? 'bg-emerald-200 text-emerald-900' : 'bg-red-200 text-red-900'
                      : si.passing ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-600'
                  }`}>
                    {si.passing ? '达标' : '未达标'}
                  </span>
                )}
                <span className={`ml-1.5 ${i === currentStudentIdx ? 'text-indigo-200' : siLabel.cls}`}>
                  {si.avg > 0 ? siLabel.text : ''}
                </span>
                {si.hasDraft && (
                  <span className={`ml-1 text-[10px] ${i === currentStudentIdx ? 'text-indigo-200' : 'text-green-600'}`}>✓</span>
                )}
                {si.deliveryStatus === 'delivered' && (
                  <span className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded ${
                    i === currentStudentIdx
                      ? 'bg-emerald-200 text-emerald-900'
                      : 'bg-emerald-100 text-emerald-700'
                  }`}>已送达</span>
                )}
                {si.deliveryStatus === 'sending' && (
                  <span className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded ${
                    i === currentStudentIdx
                      ? 'bg-amber-200 text-amber-900'
                      : 'bg-amber-100 text-amber-700'
                  }`}>发送中</span>
                )}
                {si.deliveryStatus === 'failed' && (
                  <span className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded ${
                    i === currentStudentIdx
                      ? 'bg-red-200 text-red-900'
                      : 'bg-red-100 text-red-700'
                  }`}>发送失败</span>
                )}
                {si.reviewed > 0 && !si.hasDraft && (
                  <span className={`ml-1 text-[10px] ${i === currentStudentIdx ? 'text-indigo-200' : 'text-indigo-500'}`}>
                    {si.reviewed}评
                  </span>
                )}
                {si.reviewed === 0 && si.avg === 0 && !si.hasDraft && (
                  <span className={`ml-1 text-[10px] ${i === currentStudentIdx ? 'text-indigo-200' : 'text-slate-400'}`}>未评</span>
                )}
              </button>
            );
          })}
        </div>
        <button
          onClick={batchGenerate}
          disabled={batchLoading}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 text-white font-medium cursor-pointer hover:bg-emerald-700 disabled:opacity-50 transition-colors shrink-0"
        >
          {batchLoading ? '生成中...' : '一键生成全部评语'}
        </button>
      </div>

      {/* Batch result banner */}
      {batchResult && (
        <div className={`rounded-xl p-3 border text-xs ${batchResult.results?.some(r => !r.error) ? 'bg-green-50 border-green-200 text-green-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
          <div className="font-medium mb-1">批量生成完成</div>
          <div className="flex flex-wrap gap-2">
            {batchResult.results?.map(r => (
              <span key={r.student_id} className={r.error ? 'text-amber-600' : 'text-green-700'}>
                {r.student_name}: {r.error ? r.error : '✓'}
              </span>
            ))}
          </div>
          <button onClick={() => setBatchResult(null)} className="text-[10px] text-slate-400 hover:text-slate-600 cursor-pointer mt-1">关闭</button>
        </div>
      )}

      {/* ── Main content ──────────────────────────────────── */}
      {!student && <div className="text-slate-400 text-center py-10">请选择学生</div>}
      {student && (
        <div className="grid grid-cols-2 gap-5">
          {/* Left: teacher data context */}
          <div className="flex flex-col gap-4">
            <div className="bg-white rounded-xl p-4 border border-slate-200">
              <div className="text-sm font-semibold text-slate-600 mb-3">评估结果摘要</div>
              <div className={`text-xl font-bold mb-2 ${rl.cls}`}>
                {rl.text} <span className="text-sm text-slate-400">（均分 {studentAvg.toFixed(1)}/4.0）</span>
                {rl.upgraded && <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">加分项↑</span>}
              </div>
              {studentAvg > 0 && (
                <div className={`mb-2 text-xs font-medium px-2.5 py-1.5 rounded-lg border ${
                  studentPassing
                    ? 'bg-green-50 border-green-200 text-green-700'
                    : 'bg-red-50 border-red-200 text-red-700'
                }`}>
                  {studentPassing
                    ? `✅ 已达合格线（${student.grade <= 3 ? '1-3年级' : '4-6年级'} ≥${passLineForGrade(student.grade).toFixed(1)}）`
                    : `⚠️ 未达合格线（${student.grade <= 3 ? '1-3年级' : '4-6年级'} ≥${passLineForGrade(student.grade).toFixed(1)}）`}
                </div>
              )}
              <div className="text-xs text-slate-500 mb-2">
                教师已批改 <b className="text-indigo-600">{reviewedCount}</b> / {topicCount} 个辩题
              </div>
            </div>

            <div className="bg-white rounded-xl p-4 border border-slate-200">
              <div className="text-sm font-semibold text-slate-600 mb-3">教师批改记录</div>
              {topicDetails.length === 0 && (
                <div className="text-xs text-slate-400">暂无评估数据</div>
              )}
              {topicDetails.map(({ topic, avg, scores, isReviewed, tags, note }) => (
                <div key={topic.id} className="mb-3 pb-3 border-b border-slate-50 last:border-0 last:pb-0 last:mb-0">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-xs font-medium text-slate-700">辩题{topic.order}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${isReviewed ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-100 text-slate-500'}`}>
                      {isReviewed ? '已批改' : '仅AI'}
                    </span>
                    <span className={`text-[10px] ${ratingLabel(avg, [], student.grade).cls}`}>{ratingLabel(avg, [], student.grade).text}</span>
                    {isReviewed && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                        avg >= passLineForGrade(student.grade) ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-600'
                      }`}>
                        {avg >= passLineForGrade(student.grade) ? '达标' : '未达标'}
                      </span>
                    )}
                  </div>
                  {scores && (
                    <div className="flex gap-1 flex-wrap mb-1.5">
                      {Object.entries(scores).map(([dim, rating]) => (
                        <span key={dim} className="text-[10px] bg-slate-50 text-slate-600 px-1.5 py-0.5 rounded">
                          {DIM_LABELS[dim] || dim} {rating}
                        </span>
                      ))}
                    </div>
                  )}
                  {tags.length > 0 && (
                    <div className="flex gap-1 flex-wrap mb-1">
                      {tags.map(tag => (
                        <span key={tag} className="text-[10px] bg-indigo-50 text-indigo-600 border border-indigo-200 px-1.5 py-0.5 rounded">{tag}</span>
                      ))}
                    </div>
                  )}
                  {note && (
                    <div className="text-[11px] text-indigo-700 bg-indigo-50/50 rounded px-2 py-1 leading-relaxed">{note}</div>
                  )}
                  {!isReviewed && !tags.length && !note && (
                    <div className="text-[10px] text-slate-400">（教师尚未批改此题，评语将仅参考AI评分）</div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Right: comment editor */}
          <div className="flex flex-col gap-3">
            <div className="flex justify-between items-center">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-slate-800">{student.name} 的评语</h3>
                {DELIVERY_LABEL[currentDeliveryStatus] && (
                  <span
                    className={`text-[11px] font-medium px-2 py-0.5 rounded-full border ${DELIVERY_LABEL[currentDeliveryStatus].cls}`}
                    title={student.comment_delivery_error || ''}
                  >
                    {DELIVERY_LABEL[currentDeliveryStatus].text}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                {saveStatus === 'saving' && <span className="text-[10px] text-slate-400">保存中...</span>}
                {saveStatus === 'saved' && <span className="text-[10px] text-green-600">已自动保存</span>}
                {draft && !loading && (
                  <button onClick={generate} disabled={loading}
                    className="text-xs bg-slate-50 text-slate-500 border border-slate-200 rounded-md px-2.5 py-1 cursor-pointer hover:bg-slate-100 disabled:opacity-50">
                    {loading ? '生成中...' : '重新生成'}
                  </button>
                )}
              </div>
            </div>

            {!draft && !loading && (
              <div className="flex-1 flex flex-col items-center justify-center min-h-[280px] bg-slate-50 rounded-xl border border-dashed border-slate-300">
                <div className="text-slate-400 text-sm mb-4 text-center px-8 leading-relaxed">
                  {reviewedCount > 0
                    ? `已批改 ${reviewedCount} 个辩题，可以生成评语了`
                    : '请先在「评分」页面完成至少一个辩题的教师批改'}
                </div>
                <button
                  onClick={generate}
                  disabled={reviewedCount === 0 || loading}
                  className="px-6 py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-medium cursor-pointer hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  生成评语
                </button>
              </div>
            )}

            {loading && (
              <div className="flex-1 flex flex-col items-center justify-center min-h-[280px] bg-slate-50 rounded-xl border border-dashed border-indigo-300">
                <div className="text-indigo-500 text-sm animate-pulse">AI 正在根据批改记录生成评语...</div>
              </div>
            )}

            {draft && !loading && (
              <textarea
                value={draft}
                onChange={handleDraftChange}
                className="flex-1 text-sm leading-7 border border-slate-200 rounded-xl p-4 resize-none outline-none min-h-[280px] focus:ring-1 focus:ring-indigo-300"
              />
            )}

            {draft && !loading && (
              <div className="flex flex-col items-end gap-2">
                {sendStatus.message && (
                  <div className={`text-xs ${
                    sendStatus.kind === 'error'
                      ? 'text-red-600'
                      : sendStatus.kind === 'delivery_failed'
                        ? 'text-red-600'
                      : sendStatus.kind === 'card_sent'
                        ? 'text-green-700'
                        : sendStatus.kind === 'delivered'
                          ? 'text-green-700'
                        : sendStatus.kind === 'pending'
                          ? 'text-amber-700'
                          : 'text-slate-500'
                  }`}>
                    {sendStatus.message}
                  </div>
                )}
                <button
                  onClick={send}
                  disabled={!draft.trim() || ['sending', 'card_sent', 'delivery_sending', 'delivered'].includes(sendStatus.kind)}
                  className="px-5 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium cursor-pointer hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {sendButtonText}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
