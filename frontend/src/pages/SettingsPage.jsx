import { useEffect, useMemo, useState } from 'react';
import * as api from '../api/client';

const LLM_PROVIDERS = [
  { id: 'dashscope', label: 'DashScope 百炼' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'anthropic', label: 'Anthropic（Claude）' },
  { id: 'custom', label: '自定义（OpenAI 兼容接口）' },
];

const ASR_PROVIDERS = [
  { id: 'mock', label: '演示转写（mock）' },
  { id: 'qwen_asr', label: '百炼 qwen3-asr-flash（推荐）' },
  { id: 'openai', label: 'OpenAI 兼容（whisper）' },
  { id: 'dashscope', label: 'DashScope 百炼（paraformer）' },
];

const SECTIONS = [
  {
    key: 'llm',
    title: '主评估 LLM',
    icon: '🧠',
    desc: '用于 AI 维度评估、评语草稿和备课总结。',
    fields: [
      { key: 'llm_provider', label: '提供商', type: 'select', options: LLM_PROVIDERS },
      { key: 'llm_api_key', label: 'API Key', type: 'password', secret: true },
      { key: 'llm_model', label: '模型', type: 'text', placeholder: '如 deepseek-chat / qwen-plus / claude-3-5-sonnet-20241022' },
      { key: 'llm_base_url', label: 'Base URL（可选）', type: 'text', placeholder: '留空使用提供商默认地址；自定义接口请填完整地址' },
    ],
  },
  {
    key: 'asr',
    title: '语音转写 ASR',
    icon: '🎙️',
    desc: '课堂录音转写。qwen_asr 会复用 LLM Base URL 与 Key。',
    fields: [
      { key: 'asr_provider', label: '提供商', type: 'select', options: ASR_PROVIDERS },
      { key: 'asr_api_key', label: 'ASR API Key', type: 'password', secret: true, hint: '留空则复用上方 LLM API Key' },
      { key: 'asr_model', label: '模型', type: 'text', placeholder: '如 qwen3-asr-flash / whisper-1' },
    ],
  },
  {
    key: 'feishu_bot',
    title: '飞书机器人',
    icon: '🤖',
    desc: '企业自建应用凭据，用于评语卡片与消息推送。',
    fields: [
      { key: 'feishu_app_id', label: 'App ID', type: 'text', placeholder: 'cli_...' },
      { key: 'feishu_app_secret', label: 'App Secret', type: 'password', secret: true },
      { key: 'feishu_teacher_open_id', label: '教师 open_id', type: 'text', placeholder: 'ou_...（可用后端 resolve_open_id 解析）' },
      { key: 'feishu_web_base_url', label: '网页地址（卡片跳转）', type: 'text', placeholder: 'https://你的前端地址' },
      { key: 'feishu_verification_token', label: 'Verification Token', type: 'password', secret: true },
      { key: 'feishu_encrypt_key', label: 'Encrypt Key', type: 'password', secret: true },
    ],
  },
  {
    key: 'feishu_bitable',
    title: '飞书多维表格',
    icon: '📊',
    desc: '评估数据同步到多维表格（班级 / 辩题 / 学生 / 评估记录 / 讲评计划）。',
    fields: [
      { key: 'feishu_bitable_app_token', label: 'App Token', type: 'text', placeholder: 'Bitable 应用的 app_token' },
      { key: 'feishu_bitable_table_ids', label: '表格 ID（JSON）', type: 'textarea', placeholder: '{"courses":"...","topics":"...","students":"...","responses":"...","prep_plans":"..."}' },
    ],
  },
];

const JSON_EXAMPLE = {
  llm_provider: 'anthropic',
  llm_api_key: 'sk-ant-...',
  llm_model: 'claude-3-5-sonnet-20241022',
  llm_base_url: 'https://api.anthropic.com/v1',
};

function Field({ field, value, onChange, hasValue }) {
  if (field.type === 'select') {
    return (
      <select
        value={value || ''}
        onChange={(e) => onChange(field.key, e.target.value)}
        className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
      >
        {(field.options || []).map((o) => (
          <option key={o.id} value={o.id}>{o.label}</option>
        ))}
      </select>
    );
  }
  if (field.type === 'textarea') {
    return (
      <textarea
        value={value || ''}
        onChange={(e) => onChange(field.key, e.target.value)}
        placeholder={field.placeholder}
        rows={3}
        className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
      />
    );
  }
  const isSecret = !!field.secret;
  return (
    <input
      type={field.type === 'password' ? 'password' : 'text'}
      value={value || ''}
      onChange={(e) => onChange(field.key, e.target.value)}
      placeholder={isSecret && hasValue ? '已配置（留空则不修改）' : field.placeholder}
      className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
    />
  );
}

export default function SettingsPage() {
  const [form, setForm] = useState({});
  const [hasValue, setHasValue] = useState({});
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState({});       // { sectionKey: 'save' | 'test' | null }
  const [sectionMsg, setSectionMsg] = useState({});
  const [jsonText, setJsonText] = useState('');
  const [jsonMsg, setJsonMsg] = useState('');

  const applyResponse = (s) => {
    setMeta(s);
    const init = {};
    const has = {};
    Object.entries(s.items || {}).forEach(([k, item]) => {
      has[k] = item.has_value;
      init[k] = item.secret ? '' : (item.value || '');
    });
    setForm(init);
    setHasValue(has);
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await api.getSettings();
        if (alive) applyResponse(s);
      } catch (e) {
        if (alive) setSectionMsg((m) => ({ ...m, global: e?.response?.data?.detail || e?.message || '无法加载设置' }));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  const setField = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  const statusBadges = useMemo(() => {
    if (!meta) return [];
    return [
      ['主评估 LLM', meta.llm_configured],
      ['语音转写 ASR', meta.asr_configured],
      ['飞书机器人', meta.feishu_configured],
      ['多维表格', meta.bitable_configured],
    ];
  }, [meta]);

  const saveSection = async (sectionKey) => {
    const section = SECTIONS.find((s) => s.key === sectionKey);
    setBusy((b) => ({ ...b, [sectionKey]: 'save' }));
    setSectionMsg((m) => ({ ...m, [sectionKey]: '' }));
    try {
      const updates = {};
      section.fields.forEach((f) => {
        const v = (form[f.key] || '').trim();
        if (v) updates[f.key] = v;
      });
      const s = await api.updateSettings(updates);
      applyResponse(s);
      setSectionMsg((m) => ({ ...m, [sectionKey]: s.demo ? '演示模式无法保存' : '已保存并立即生效。' }));
    } catch (e) {
      setSectionMsg((m) => ({ ...m, [sectionKey]: e?.response?.data?.detail || e?.message || '保存失败' }));
    } finally {
      setBusy((b) => ({ ...b, [sectionKey]: null }));
    }
  };

  const testSection = async (sectionKey) => {
    setBusy((b) => ({ ...b, [sectionKey]: 'test' }));
    setSectionMsg((m) => ({ ...m, [sectionKey]: '' }));
    try {
      const r = await api.testSettings(sectionKey);
      setSectionMsg((m) => ({ ...m, [sectionKey]: (r.ok ? '✅ ' : '❌ ') + (r.detail || (r.ok ? '通过' : '失败')) }));
    } catch (e) {
      setSectionMsg((m) => ({ ...m, [sectionKey]: '❌ ' + (e?.response?.data?.detail || e?.message || '测试失败') }));
    } finally {
      setBusy((b) => ({ ...b, [sectionKey]: null }));
    }
  };

  const applyJson = async () => {
    setJsonMsg('');
    let obj;
    try {
      obj = JSON.parse(jsonText);
    } catch {
      setJsonMsg('JSON 格式错误');
      return;
    }
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
      setJsonMsg('JSON 必须是一个对象');
      return;
    }
    try {
      const s = await api.updateSettings(obj);
      applyResponse(s);
      setJsonMsg(s.demo ? '演示模式无法保存' : 'JSON 配置已应用并立即生效。');
    } catch (e) {
      setJsonMsg(e?.response?.data?.detail || e?.message || '应用失败');
    }
  };

  if (loading) {
    return <div className="text-center text-slate-400 py-20">加载设置中…</div>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="bg-white rounded-xl p-5 border border-slate-200">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-800">系统设置</h2>
            <p className="text-xs text-slate-500 mt-1">
              每一栏独立保存、独立测试；密钥不回显完整内容，留空即不修改。
            </p>
          </div>
          <div className="flex gap-2 flex-wrap">
            {statusBadges.map(([label, ok]) => (
              <span
                key={label}
                className={`text-[11px] px-2 py-1 rounded-full border font-medium ${
                  ok ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-50 text-slate-500 border-slate-200'
                }`}
              >
                {label}：{ok ? '已配置' : '未配置'}
              </span>
            ))}
          </div>
        </div>
      </div>

      {meta?.demo && (
        <div className="bg-amber-50 border border-amber-200 text-amber-700 text-sm rounded-xl px-4 py-3">
          当前是纯前端演示模式（无后端），设置无法保存。请切换到「真实」模式后在此填写。
        </div>
      )}

      {SECTIONS.map((section) => (
        <div key={section.key} className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-100 flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-slate-800">
                <span className="mr-2">{section.icon}</span>{section.title}
              </h3>
              <p className="text-xs text-slate-400 mt-0.5">{section.desc}</p>
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                onClick={() => saveSection(section.key)}
                disabled={!!busy[section.key]}
                className="px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {busy[section.key] === 'save' ? '保存中…' : '保存本栏'}
              </button>
              <button
                onClick={() => testSection(section.key)}
                disabled={!!busy[section.key]}
                className="px-3 py-1.5 text-xs font-medium rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 disabled:opacity-50"
              >
                {busy[section.key] === 'test' ? '测试中…' : '测试本栏'}
              </button>
            </div>
          </div>
          <div className="px-5 py-4 grid grid-cols-1 md:grid-cols-2 gap-4">
            {section.fields.map((field) => (
              <div key={field.key} className={field.type === 'textarea' ? 'md:col-span-2' : ''}>
                <label className="block text-xs font-medium text-slate-600 mb-1.5">{field.label}</label>
                <Field
                  field={field}
                  value={form[field.key]}
                  hasValue={hasValue[field.key]}
                  onChange={setField}
                />
                {field.hint && <p className="text-[11px] text-slate-400 mt-1">{field.hint}</p>}
              </div>
            ))}
          </div>
          {sectionMsg[section.key] && (
            <div className="px-5 pb-3 text-xs text-slate-600">{sectionMsg[section.key]}</div>
          )}
        </div>
      ))}

      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-100">
          <h3 className="text-sm font-semibold text-slate-800">高级：JSON 配置</h3>
          <p className="text-xs text-slate-400 mt-0.5">
            粘贴一个扁平 JSON 对象（键名同上方字段，如 llm_provider / llm_api_key / llm_model …），可直接配置任意提供商的模型。
          </p>
        </div>
        <div className="px-5 py-4">
          <textarea
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            placeholder={JSON.stringify(JSON_EXAMPLE, null, 2)}
            rows={6}
            className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
          />
          <div className="flex items-center gap-3 mt-2">
            <button
              onClick={applyJson}
              className="px-4 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700"
            >
              应用 JSON
            </button>
            {jsonMsg && <span className="text-xs text-slate-600">{jsonMsg}</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
