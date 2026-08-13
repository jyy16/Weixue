import { useCallback, useEffect, useMemo, useState } from 'react';
import useStore from '../stores/gradingStore';
import { bandForGrade, passLineForGrade, ratingToNumber, upgradeBand, quickRatingMeta } from '../utils/ratings';
import * as api from '../api/client';

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

const barColor = (val) => {
  if (val >= 3.5) return 'bg-green-500';
  if (val >= 3) return 'bg-emerald-400';
  if (val >= 2.5) return 'bg-yellow-400';
  if (val > 0) return 'bg-orange-400';
  return 'bg-red-500';
};

const scoreLabel = (avg, grade) => {
  const passLine = grade ? passLineForGrade(grade) : 2.5;
  if (avg >= 3.5) return { text: '优秀', cls: 'text-green-600' };
  if (avg >= passLine) return { text: '良好', cls: 'text-emerald-600' };
  if (avg >= 1.5) return { text: '待提升', cls: 'text-yellow-600' };
  if (avg > 0) return { text: '薄弱', cls: 'text-red-600' };
  return { text: '未评', cls: 'text-slate-400' };
};

const ratingColor = (v) => {
  if (v >= 3.5) return '#16a34a';
  if (v >= 2.5) return '#10b981';
  if (v >= 1.5) return '#eab308';
  return v > 0 ? '#f97316' : '#94a3b8';
};

const BAND_TEXT_CLS = {
  优秀: 'text-green-600',
  良好: 'text-emerald-600',
  待提升: 'text-yellow-600',
  薄弱: 'text-red-600',
};

/** Pure-SVG radar chart for up to 9 dimension averages (0-4 scale). */
function RadarChart({ data, labels }) {
  const keys = Object.keys(data || {});
  if (keys.length < 3) {
    return <div className="text-xs text-slate-400 py-8 text-center">维度数据不足（至少 3 个维度）</div>;
  }
  const size = 340;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 52;
  const angle = (i) => (Math.PI * 2 * i) / keys.length - Math.PI / 2;
  const ring = (frac) =>
    keys.map((_, i) => `${cx + r * Math.cos(angle(i)) * frac},${cy + r * Math.sin(angle(i)) * frac}`).join(' ');
  const polygon = keys
    .map((k, i) => {
      const v = Math.min(Math.max(data[k] || 0, 0), 4);
      return `${cx + r * Math.cos(angle(i)) * (v / 4)},${cy + r * Math.sin(angle(i)) * (v / 4)}`;
    })
    .join(' ');
  return (
    <svg width="100%" viewBox={`0 0 ${size} ${size}`} className="max-w-[360px] mx-auto block">
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <polygon key={f} points={ring(f)} fill="none" stroke="#e2e8f0" strokeWidth={1} />
      ))}
      {keys.map((k, i) => {
        const x1 = cx + r * Math.cos(angle(i));
        const y1 = cy + r * Math.sin(angle(i));
        const lx = cx + (r + 36) * Math.cos(angle(i));
        const ly = cy + (r + 36) * Math.sin(angle(i));
        const mx = cx + (r / 2) * Math.cos(angle(i));
        const my = cy + (r / 2) * Math.sin(angle(i));
        const v = Math.min(Math.max(data[k] || 0, 0), 4);
        return (
          <g key={k}>
            <line x1={cx} y1={cy} x2={x1} y2={y1} stroke="#e2e8f0" strokeWidth={1} />
            <text x={lx} y={ly} textAnchor="middle" dominantBaseline="middle" fontSize={11} fill="#64748b">
              {labels[k] || k}
            </text>
            <text x={mx} y={my - 5} textAnchor="middle" fontSize={10} fill={ratingColor(v)}>
              {v.toFixed(1)}
            </text>
          </g>
        );
      })}
      <polygon points={polygon} fill="rgba(99,102,241,0.22)" stroke="#6366f1" strokeWidth={2} strokeLinejoin="round" />
    </svg>
  );
}

function ParentReportView({ report, loading, onBack }) {
  if (loading) {
    return <div className="text-slate-400 py-16 text-center">正在生成家长报告...</div>;
  }
  if (!report || report.error) {
    return (
      <div className="bg-white rounded-xl p-8 border border-slate-200 text-center">
        <div className="text-red-500 mb-3">报告加载失败</div>
        <button onClick={onBack} className="text-xs text-indigo-600 hover:text-indigo-800 cursor-pointer">返回班级报告</button>
      </div>
    );
  }
  if (!report.has_report) {
    return (
      <div className="bg-white rounded-xl p-8 border border-slate-200 text-center">
        <div className="text-lg font-semibold text-slate-700 mb-2">{report.name} 的学情报告</div>
        <div className="text-slate-400 text-sm mb-4">该学生暂未完成评估，暂无报告内容</div>
        <button onClick={onBack} className="text-xs text-indigo-600 hover:text-indigo-800 cursor-pointer">返回班级报告</button>
      </div>
    );
  }

  const dims = Object.entries(report.dimensions || {});
  const bonus = report.bonus_flags || [];
  const finalRating = upgradeBand(report.rating, bonus);
  const quick = quickRatingMeta(report.quick_rating);
  return (
    <div className="flex flex-col gap-5">
      <div className="bg-white rounded-xl p-5 border border-slate-200 flex items-center justify-between">
        <div>
          <div className="text-lg font-bold text-slate-800">{report.name} 的学情报告</div>
          <div className="text-xs text-slate-400 mt-0.5">
            {report.grade}年级 · {report.topic_title || '思辨课堂'}
            {report.reviewed ? ' · 已由教师确认' : ' · 待教师确认'}
            {report.passing !== undefined && (
              <span className={`ml-2 px-1.5 py-0.5 rounded text-[10px] ${report.passing ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                {report.passing ? '✅ 达标' : `⚠️ 未达标（合格线 ${report.pass_line}）`}
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onBack}
          className="text-xs px-3 py-1.5 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50 cursor-pointer"
        >
          ← 返回班级报告
        </button>
      </div>

      <div className="grid grid-cols-2 gap-5">
        <div className="bg-white rounded-xl p-5 border border-slate-200">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">思辨能力画像</h3>
          {dims.length === 0 ? (
            <div className="text-xs text-slate-400">暂无维度评分</div>
          ) : (
            <div className="space-y-2.5">
              {dims.map(([label, rating]) => {
                const v = ratingToNumber(rating);
                return (
                  <div key={label} className="flex items-center gap-3">
                    <div className="w-24 text-xs text-slate-600 shrink-0">{label}</div>
                    <div className="flex-1 bg-slate-100 rounded h-3.5 overflow-hidden">
                      <div
                        className="h-full rounded transition-all"
                        style={{ width: `${v !== null ? Math.max((v / 4) * 100, 3) : 0}%`, backgroundColor: ratingColor(v ?? 0) }}
                      />
                    </div>
                    <div className="w-10 text-xs text-right font-semibold text-slate-600">{rating}</div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-4">
          <div className="bg-white rounded-xl p-5 border border-slate-200">
            <h3 className="text-sm font-semibold text-slate-800 mb-2">亮点回答（得分最高）</h3>
            {report.best_answer ? (
              <div>
                <div className="text-xs text-slate-400 mb-1.5">
                  {report.best_answer.topic_title ? `辩题：${report.best_answer.topic_title}` : ''}
                  {report.best_answer.score != null && ` · 均分 ${report.best_answer.score.toFixed(1)}/4.0`}
                </div>
                <p className="text-sm text-slate-600 leading-relaxed whitespace-pre-wrap">{report.best_answer.text}</p>
              </div>
            ) : (
              <p className="text-sm text-slate-400">暂无作答记录。</p>
            )}
          </div>
          <div className="bg-white rounded-xl p-5 border border-slate-200">
            <h3 className="text-sm font-semibold text-slate-800 mb-2">综合评价</h3>
            <div className={`text-lg font-bold ${BAND_TEXT_CLS[finalRating] || 'text-slate-400'}`}>
              {finalRating || '待评定'}
            </div>
            {quick && (
              <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
                <span className={`text-[11px] px-2 py-0.5 rounded-full border font-medium ${quick.cls}`}>
                  {quick.short} 课堂即时评级：{quick.label}
                </span>
                <span className="text-[10px] text-slate-400">教师课堂第一印象，与五维度评分互补</span>
              </div>
            )}
            {bonus.length > 0 && (
              <div className="mt-1.5 text-[11px] text-amber-600 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1">
                加分项：{bonus.join('、')} → 综合评级已升级
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="bg-white rounded-xl p-5 border border-slate-200">
        <h3 className="text-sm font-semibold text-slate-800 mb-2">教师评语</h3>
        <p className="text-sm text-slate-600 leading-relaxed whitespace-pre-wrap">
          {report.teacher_comment || '暂未填写评语。'}
        </p>
      </div>
    </div>
  );
}

export default function ReportPage() {
  const { courseId, responses } = useStore();
  const [parentSid, setParentSid] = useState(null);
  const [parentReport, setParentReport] = useState(null);
  const [parentLoading, setParentLoading] = useState(false);
  const [report, setReport] = useState(null);
  const [reportLoading, setReportLoading] = useState(true);

  // 报告数据来自后端 /report（唯一口径）；demo 模式由 demoClient 用同一套
  // computeClassReport 计算。作答变化（教师改分/标签）时自动重新拉取。
  const reportVersion = useMemo(() => {
    const parts = [];
    Object.keys(responses).sort().forEach(sid => {
      (responses[sid] || []).forEach(r => {
        parts.push(
          `${r.id}:${r.teacher_reviewed ? 1 : 0}:`
          + `${r.teacher_rating || ''}:`
          + `${JSON.stringify(r.teacher_dimension_scores || null)}:`
          + `${(r.teacher_tags || []).join(',')}`,
        );
      });
    });
    return parts.join('|');
  }, [responses]);

  const loadReport = useCallback(async () => {
    if (!courseId) return;
    setReportLoading(true);
    try {
      setReport(await api.getClassReport(courseId));
    } catch (e) {
      console.warn('获取班级报告失败，请检查后端 /report 接口。', e);
      setReport(null);
    }
    setReportLoading(false);
  }, [courseId]);

  useEffect(() => {
    loadReport();
  }, [courseId, reportVersion, loadReport]);

  const openParentReport = async (sid) => {
    setParentSid(sid);
    setParentLoading(true);
    setParentReport(null);
    try {
      setParentReport(await api.getStudentReport(sid));
    } catch {
      setParentReport({ error: true });
    }
    setParentLoading(false);
  };

  if (parentSid) {
    return (
      <ParentReportView
        report={parentReport}
        loading={parentLoading}
        onBack={() => {
          setParentSid(null);
          setParentReport(null);
        }}
      />
    );
  }

  if (!courseId) {
    return <div className="text-slate-400 py-10 text-center">加载报告...</div>;
  }
  if (reportLoading && !report) {
    return <div className="text-slate-400 py-10 text-center">正在加载班级报告...</div>;
  }
  if (!report || report.student_count === 0) {
    return <div className="text-red-500 py-10 text-center">报告加载失败：暂无班级数据</div>;
  }

  const classLabel = scoreLabel(report.class_avg);

  return (
    <div className="flex flex-col gap-5">
      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: '参评人数', value: `${report.student_count}人`, color: 'text-slate-600' },
          { label: '班级均分', value: `${report.class_avg.toFixed(1)}/4.0`, color: classLabel.cls },
          {
            label: '达标人数',
            value: `${report.pass_count ?? 0}人 · ${Math.round((report.pass_rate ?? 0) * 100)}%`,
            color: 'text-emerald-600',
          },
          { label: '辩题数', value: report.topic_stats.length, color: 'text-slate-600' },
        ].map((c, i) => (
          <div key={i} className="bg-white rounded-xl p-4 border border-slate-200">
            <div className="text-slate-400 text-xs">{c.label}</div>
            <div className={`text-xl font-bold mt-1 ${c.color}`}>{c.value}</div>
          </div>
        ))}
      </div>
      <div className="text-[11px] text-slate-400 -mt-3">
        合格线：1-3年级 ≥2.5（B+），4-6年级及以上 ≥3.0（A-），按学生各自年级判断达标。
      </div>

      {/* 课堂即时评级（教师第一印象）——与五维度完整评分互补 */}
      {(() => {
        const qc = report.quick_rating_counts || {};
        const total = (qc.good || 0) + (qc.guide || 0) + (qc.echo || 0);
        if (!total) return null;
        return (
          <div className="bg-white rounded-xl p-5 border border-slate-200">
            <div className="flex items-center gap-2 mb-3">
              <span className="text-lg">🎯</span>
              <h3 className="text-sm font-semibold text-slate-800">课堂即时评级（教师第一印象）</h3>
              <span className="text-[11px] bg-blue-50 text-blue-700 border border-blue-200 px-1.5 py-0.5 rounded">共 {total} 次</span>
            </div>
            <div className="grid grid-cols-3 gap-3">
              {[
                ['good', '表达完整', '👍'],
                ['guide', '需引导', '➕'],
                ['echo', '复述/未表达', '⚠️'],
              ].map(([key, label, icon]) => {
                const q = quickRatingMeta(key);
                return (
                  <div key={key} className={`rounded-lg border px-3 py-2 ${q.cls}`}>
                    <div className="text-[11px]">{icon} {label}</div>
                    <div className="text-xl font-bold mt-0.5">{qc[key] || 0} 次</div>
                  </div>
                );
              })}
            </div>
            <div className="text-[11px] text-slate-400 mt-2">
              课堂中老师一键留下的轻量判断（绿=表达完整 / 黄=需引导 / 红=复述或未表达），与工作台五维度完整评分互补、不冲突。
            </div>
          </div>
        );
      })()}

      {/* Class-level radar */}
      <div className="bg-white rounded-xl p-5 border border-slate-200">
        <h3 className="text-sm font-semibold text-slate-800 mb-2">班级思辨能力雷达</h3>
        <div className="text-[11px] text-slate-400 mb-1">基于全班已确认评分的维度均值（0-4 分）</div>
        <RadarChart data={report.class_dim_avg} labels={DIM_LABELS} />
      </div>

      {/* Per-topic dimension breakdown */}
      <div className="bg-white rounded-xl p-5 border border-slate-200">
        <h3 className="text-sm font-semibold text-slate-800 mb-4">各辩题维度均分</h3>
        {report.topic_stats.map(ts => (
          <div key={ts.topic_id} className="mb-4 pb-3 border-b border-slate-50 last:border-0 last:pb-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-sm font-medium text-slate-700">{ts.title}</span>
              <span className="text-[10px] bg-slate-100 text-slate-500 px-1.5 py-0.5 rounded">{ts.cognitive_tier}</span>
              {ts.uncertain > 0 && <span className="text-[10px] text-slate-400">{ts.uncertain}人存疑</span>}
            </div>
            {Object.entries(ts.avg_dimension_scores).length > 0 ? (
              <div className="space-y-1.5">
                {Object.entries(ts.avg_dimension_scores).map(([dim, val]) => (
                  <div key={dim} className="flex items-center gap-3">
                    <div className="w-20 text-xs text-slate-500 shrink-0">{DIM_LABELS[dim] || dim}</div>
                    <div className="flex-1 bg-slate-100 rounded h-4 overflow-hidden">
                      <div className={`h-full rounded transition-all ${barColor(val)}`}
                        style={{ width: `${Math.max((val / 4) * 100, 2)}%` }} />
                    </div>
                    <div className={`w-12 text-xs text-right font-semibold ${barColor(val).replace('bg-', 'text-')}`}>{val.toFixed(1)}</div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-slate-400">暂无评估数据</div>
            )}
          </div>
        ))}
      </div>

      {/* Per-student scores */}
      <div className="bg-white rounded-xl p-5 border border-slate-200">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">学生个体评估</h3>
        <div className="grid grid-cols-3 gap-2">
          {report.student_stats.map(s => {
            const sl = scoreLabel(s.avg_score, s.grade);
            const band = upgradeBand(bandForGrade(s.avg_score, s.grade), s.bonus_flags || []);
            const upgraded = band !== sl.text && s.avg_score > 0;
            return (
              <div key={s.student_id} className="p-3 rounded-lg bg-slate-50 border border-slate-100">
                <div className="flex justify-between items-center mb-1.5">
                  <div>
                    <span className="text-sm font-medium text-slate-700">{s.name}</span>
                    <span className="text-[10px] text-slate-400 ml-1.5">{s.grade}年级</span>
                    {s.avg_score > 0 && (
                      <span className={`ml-1.5 text-[9px] px-1 py-0.5 rounded ${s.passing ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                        {s.passing ? '达标' : '未达标'}
                      </span>
                    )}
                    {upgraded && <span className="ml-1.5 text-[9px] px-1 py-0.5 rounded bg-amber-100 text-amber-700">加分项↑</span>}
                    {s.quick_ratings && Object.entries(s.quick_ratings).filter(([, n]) => n > 0).map(([k, n]) => {
                      const q = quickRatingMeta(k);
                      return q ? (
                        <span key={k} className={`ml-1 text-[9px] px-1 py-0.5 rounded ${q.cls}`}>
                          {q.short}{n}
                        </span>
                      ) : null;
                    })}
                  </div>
                  <span className={`text-sm font-bold ${sl.cls}`}>{s.avg_score > 0 ? s.avg_score.toFixed(1) : '-'}</span>
                </div>
                <div className="text-[11px] text-slate-400 mt-1">
                  {band} · {s.cognitive_tier === 'basic' ? '基础层' : s.cognitive_tier === 'developing' ? '发展层' : '进阶层'}
                  {s.uncertain > 0 ? ` · ${s.uncertain}题存疑` : ''}
                  {s.pass_line ? ` · 合格线 ≥${s.pass_line.toFixed(1)}` : ''}
                </div>
                <button
                  onClick={() => openParentReport(s.student_id)}
                  className="mt-2 text-[11px] px-2 py-1 rounded-md bg-indigo-50 text-indigo-600 hover:bg-indigo-100 cursor-pointer w-full"
                >
                  查看家长报告
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Top tags */}
      {report.top_tags.length > 0 && (
        <div className="bg-white rounded-xl p-5 border border-slate-200">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">高频标签</h3>
          {report.top_tags.filter(t => t.count > 0).map((t, i) => (
            <div key={t.name} className="flex items-start gap-2.5 py-2 border-b border-slate-50 last:border-0">
              <span className="bg-red-100 text-red-700 text-[11px] font-semibold px-2 py-0.5 rounded shrink-0">{t.count}次</span>
              <div>
                <div className="text-sm text-slate-800">{t.name}</div>
                <div className="text-[11px] text-slate-400">{t.source === 'ai_new' ? 'AI新增标签' : '基础标签'}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
