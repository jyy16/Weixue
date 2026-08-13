import { useEffect, useState } from 'react';
import useStore from './stores/gradingStore';
import GradingPage from './pages/GradingPage';
import ManagementPage from './pages/ManagementPage';
import CommentsPage from './pages/CommentsPage';
import PrepPage from './pages/PrepPage';
import ReportPage from './pages/ReportPage';
import LibraryPage from './pages/LibraryPage';
import SettingsPage from './pages/SettingsPage';
import LiveCockpit from './pages/LiveCockpit';
import { getMode, setMode, resolveMode, subscribeModeChange } from './config/mode';

const TABS = [
  { key: 'manage',   label: '管理',     icon: '🗂️' },
  { key: 'grading',  label: '智能评估', icon: '🧠' },
  { key: 'comments', label: '评语生成', icon: '💬' },
  { key: 'prep',     label: '备课辅助', icon: '📋' },
  { key: 'report',   label: '学情报告', icon: '📊' },
  { key: 'library',  label: '标签库',   icon: '🏷️' },
  { key: 'settings', label: '设置',     icon: '⚙️' },
];

const TAB_DESC = {
  grading:  'AI已完成多维度认知评估。每题按维度给出A+/A/A-/B+/B/B-等级，请逐份审阅并修改。',
  comments: '基于您的评分和批注，AI生成评语草稿。请编辑后发送给学生。',
  prep:     '基于您确认的评估数据，AI按维度薄弱项整理讲评建议。',
  report:   '基于教师审核后的最终评分，生成班级思辨能力分析报告。',
  library:  '管理评语标签库。基础标签来自教研经验，AI新增标签由评估过程中的教师选择自动入库。',
  settings: '配置 LLM / 语音转写 / 飞书机器人 / 多维表格，保存后立即生效。',
};

const TIER_LABEL = { basic: '低年级', developing: '中年级', advancing: '高年级' };

export default function App() {
  const { course, courses, currentTab, setTab, currentMode, setMode: setAppMode, loading, assessing, assessmentProgress, loadAllCourses, selectCourse, createCourse, runAssessment, resetForModeSwitch } = useStore();
  const [runtimeMode, setRuntimeMode] = useState(getMode());
  const [resolvedMode, setResolvedMode] = useState(null);
  const [modeError, setModeError] = useState('');

  useEffect(() => {
    // Deep link support (e.g. from Feishu card jump buttons): /?tab=comments
    const t = new URLSearchParams(window.location.search).get('tab');
    if (t && TABS.some((x) => x.key === t)) setTab(t);
    loadAllCourses();
    // 演示/真实模式：自动探测一次，并监听切换（切换后重载数据）。
    resolveMode().then(setResolvedMode);
    return subscribeModeChange(async (next) => {
      setRuntimeMode(next);
      setModeError('');
      try {
        await resetForModeSwitch();
      } catch (e) {
        console.error('模式切换失败:', e);
        setModeError(
          next === 'real'
            ? '无法连接后端（/api 不可达），请确认服务已启动；页面仍显示当前数据。'
            : '加载演示数据失败。',
        );
      }
    });
  }, []);

  const displayMode = runtimeMode === 'auto' ? (resolvedMode || 'auto') : runtimeMode;
  const switchMode = (next) => {
    if (next === displayMode) return;
    setMode(next);
  };

  if (loading && !course) {
    return (
      <div className="min-h-screen flex items-center justify-center text-slate-400">
        <div className="text-center">
          <div className="text-4xl mb-4 animate-pulse">📚</div>
          <div>加载数据中...</div>
        </div>
      </div>
    );
  }

  if (!course) {
    return (
      <div className="min-h-screen flex items-center justify-center text-slate-400">
        <div className="text-center">
          <div className="text-4xl mb-4">📭</div>
          <div>暂无课程数据。请先运行 <code className="bg-slate-200 px-2 py-1 rounded text-sm">python seed.py</code> 初始化。</div>
        </div>
      </div>
    );
  }

  const handleCreateCourse = async () => {
    const title = window.prompt('班级标题（如：动物应该养在动物园吗？）');
    if (!title) return;
    const class_name = window.prompt('班级名称（如：思辨提升班）') || title;
    const grade_level = Math.max(1, Math.min(7, parseInt(window.prompt('目标年级（1-7）', '4') || '4', 10) || 4));
    await createCourse({ title, class_name, grade_level });
  };

  return (
    <div className="min-h-screen bg-slate-100">
      {/* ── Header ─────────────────────────────────────── */}
      <header className="bg-white border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-6 py-3 flex justify-between items-center">
          <div className="flex items-center gap-2">
            <span className={`text-xs font-bold px-2 py-1 rounded ${
              displayMode === 'real' ? 'bg-emerald-100 text-emerald-700' : 'bg-indigo-100 text-indigo-700'
            }`}>
              {displayMode === 'real' ? '真实模式' : displayMode === 'demo' ? '演示模式' : '自动探测…'}
            </span>
            <h1 className="text-lg font-bold text-slate-900 m-0">维学思辨星 · 少儿思辨能力评估系统</h1>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex bg-slate-100 rounded-lg p-0.5 mr-1">
              <button
                onClick={() => switchMode('demo')}
                title="使用内置演示数据，无需后端"
                className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors cursor-pointer
                  ${displayMode === 'demo' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-200'}`}
              >
                演示
              </button>
              <button
                onClick={() => switchMode('real')}
                title="连接 FastAPI 后端"
                className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors cursor-pointer
                  ${displayMode === 'real' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-200'}`}
              >
                真实
              </button>
            </div>
            <select
              value={course.id}
              onChange={e => selectCourse(parseInt(e.target.value, 10))}
              className="text-xs border border-slate-200 rounded-lg px-2 py-1.5 outline-none focus:ring-1 focus:ring-indigo-300 cursor-pointer"
            >
              {courses.map(c => (
                <option key={c.id} value={c.id}>{c.class_name} · {c.title}</option>
              ))}
            </select>
            <button
              onClick={handleCreateCourse}
              className="text-xs border border-indigo-200 bg-indigo-50 text-indigo-600 rounded-lg px-2.5 py-1.5 cursor-pointer hover:bg-indigo-100 transition-colors"
            >
              ＋ 新建班级
            </button>
          </div>
        </div>
      </header>

      {modeError && (
        <div className="bg-red-50 border-b border-red-200">
          <div className="max-w-7xl mx-auto px-6 py-2 text-xs text-red-600">{modeError}</div>
        </div>
      )}

      {/* ── Tabs ───────────────────────────────────────── */}
      <nav className="bg-white border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-6 flex gap-1 items-center">
          <div className="flex bg-slate-100 rounded-lg p-0.5 mr-2">
            <button
              onClick={() => setAppMode('live')}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors cursor-pointer
                ${currentMode === 'live' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-200'}`}
            >
              🏫 课堂
            </button>
            <button
              onClick={() => setAppMode('workbench')}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors cursor-pointer
                ${currentMode === 'workbench' ? 'bg-indigo-600 text-white' : 'text-slate-500 hover:bg-slate-200'}`}
            >
              🗂️ 工作台
            </button>
          </div>
          {currentMode === 'workbench' && TABS.map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors cursor-pointer bg-transparent
                ${currentTab === t.key
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:bg-slate-50'}`}
            >
              <span className="mr-1">{t.icon}</span>{t.label}
            </button>
          ))}
          <div className="flex-1" />
          {currentMode === 'workbench' && currentTab === 'grading' && !assessing && (
            <div className="flex gap-2 my-1.5">
              <button
                onClick={runAssessment}
                className="px-4 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
              >
                🤖 AI评估全班
              </button>
            </div>
          )}
        </div>
      </nav>

      {/* ── Assessment Progress Bar ──────────────────────── */}
      {assessing && assessmentProgress && (
        <div className="bg-indigo-50 border-b border-indigo-200">
          <div className="max-w-7xl mx-auto px-6 py-3">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <div className="w-4 h-4 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin" />
                <span className="text-sm font-medium text-indigo-800">
                  AI评估进行中 — {assessmentProgress.completed} / {assessmentProgress.total}
                </span>
              </div>
              <div className="flex items-center gap-4 text-xs text-indigo-600">
                {assessmentProgress.llm_calls > 0 && <span>LLM调用 {assessmentProgress.llm_calls} 次</span>}
                {assessmentProgress.skipped > 0 && <span>跳过 {assessmentProgress.skipped}</span>}
                {assessmentProgress.errors > 0 && <span className="text-red-500">异常 {assessmentProgress.errors}</span>}
              </div>
            </div>
            <div className="w-full bg-indigo-100 rounded-full h-2.5 overflow-hidden">
              <div
                className="bg-indigo-600 h-full rounded-full transition-all duration-300 ease-out"
                style={{ width: `${assessmentProgress.total > 0 ? (assessmentProgress.completed / assessmentProgress.total * 100) : 0}%` }}
              />
            </div>
            <div className="text-[11px] text-indigo-500 mt-1.5 text-right">
              {assessmentProgress.total > 0 ? Math.round(assessmentProgress.completed / assessmentProgress.total * 100) : 0}%
            </div>
          </div>
        </div>
      )}
      {assessing && !assessmentProgress && (
        <div className="bg-indigo-50 border-b border-indigo-200">
          <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-2">
            <div className="w-4 h-4 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin" />
            <span className="text-sm text-indigo-800">正在启动评估任务...</span>
          </div>
        </div>
      )}

      {currentMode === 'live' ? (
        <main className="max-w-[1500px] mx-auto px-4 pt-4 pb-10">
          <LiveCockpit />
        </main>
      ) : (
        <>
          {/* ── Description ──────────────────────────────── */}
          <div className="max-w-7xl mx-auto px-6 pt-4 pb-2 text-sm text-slate-500">
            {TAB_DESC[currentTab]}
          </div>

          {/* ── Content ──────────────────────────────────── */}
          <main className="max-w-7xl mx-auto px-6 pb-10">
            {currentTab === 'manage'   && <ManagementPage />}
            {currentTab === 'grading'  && <GradingPage />}
            {currentTab === 'comments' && <CommentsPage />}
            {currentTab === 'prep'     && <PrepPage />}
            {currentTab === 'report'   && <ReportPage />}
            {currentTab === 'library'  && <LibraryPage />}
            {currentTab === 'settings' && <SettingsPage />}
          </main>
        </>
      )}
    </div>
  );
}
