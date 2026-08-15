/**
 * Lightweight status bus for the live classroom.
 *
 * Demo mode (default): BroadcastChannel synchronizes student windows ↔ teacher
 * cockpit inside the same browser, so states look real-time without a backend.
 * If BroadcastChannel is unavailable we fall back to localStorage storage
 * events, which also fire across tabs.
 *
 * Real mode: keep the same publish/subscribe API but drive it with a short
 * polling fallback (the documented P1 path in 现场伴学设计_v1 §3.3): the
 * backend is the source of truth, so each poll re-reads responses and emits
 * change events — this works across devices, not just within one browser.
 */

import { resolveMode } from '../config/mode';
import { deriveResponseStatus } from './status';
import { getResponses } from '../api/client';

const PREFIX = 'weixue-live-';
let _channel = null;
let _listeners = new Set();
let _storageHandler = null;
let _pollTimer = null;
let _pollCid = null;
const _lastPollSig = new Map();

const POLL_INTERVAL_MS = 3000;

function _pollOnce(courseId) {
  getResponses(courseId)
    .then(resps => {
      (resps || []).forEach(r => {
        const status = deriveResponseStatus(r);
        const sig = [
          status,
          r.teacher_reviewed ? 1 : 0,
          JSON.stringify(r.teacher_dimension_scores || null),
          (r.raw_text || '').length,
          r.dialogue_finished || '',
        ].join('|');
        if (_lastPollSig.get(r.id) === sig) return;
        _lastPollSig.set(r.id, sig);
        _listeners.forEach(fn => {
          try {
            fn({ courseId, responseId: r.id, status, response: r });
          } catch (err) {
            console.error('[statusBus] poll listener error', err);
          }
        });
      });
    })
    .catch(() => { /* backend unreachable — keep polling, demo switch handles UI */ });
}

function _ensurePolling(courseId) {
  if (_pollCid === courseId && _pollTimer) return;
  _stopPolling();
  _pollCid = courseId;
  resolveMode().then(mode => {
    if (mode !== 'real' || _pollCid !== courseId) return;
    _lastPollSig.clear();
    _pollOnce(courseId);
    _pollTimer = setInterval(() => _pollOnce(courseId), POLL_INTERVAL_MS);
  });
}

function _stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
  _pollCid = null;
  _lastPollSig.clear();
}

function channelName(courseId) {
  return `${PREFIX}${courseId || 'default'}`;
}

function ensureChannel(courseId) {
  const name = channelName(courseId);
  if (typeof BroadcastChannel !== 'undefined') {
    // Rebuild when the course changes: a singleton bound to the first course
    // silently disconnects the cockpit from later student windows otherwise.
    if (_channel && _channel.name !== name) {
      try { _channel.close(); } catch { /* ignore */ }
      _channel = null;
    }
    if (!_channel) {
      _channel = new BroadcastChannel(name);
      _channel.onmessage = (e) => {
        const evt = e.data || {};
        _listeners.forEach(fn => {
          try { fn(evt); } catch (err) { console.error('[statusBus] listener error', err); }
        });
      };
    }
  }
  if (!_storageHandler && typeof window !== 'undefined') {
    _storageHandler = (e) => {
      if (e.key && e.key.startsWith(PREFIX) && e.newValue) {
        try {
          const evt = JSON.parse(e.newValue);
          _listeners.forEach(fn => {
            try { fn(evt); } catch (err) { console.error('[statusBus] storage listener error', err); }
          });
        } catch { /* ignore malformed payloads */ }
      }
    };
    window.addEventListener('storage', _storageHandler);
  }
  return _channel;
}

/** Subscribe to status events for a course. Returns an unsubscribe function. */
export function subscribeStatus(courseId, cb) {
  _listeners.add(cb);
  ensureChannel(courseId);
  _ensurePolling(courseId);
  return () => {
    _listeners.delete(cb);
    if (_listeners.size === 0) _stopPolling();
  };
}

/** Publish a status event: { responseId, status, studentId, payload }. */
export function publishStatus(courseId, event) {
  const payload = { ...event, courseId, ts: Date.now() };
  const channel = ensureChannel(courseId);
  if (channel) {
    try { channel.postMessage(payload); } catch (err) { console.warn('[statusBus] postMessage failed', err); }
  } else if (typeof window !== 'undefined') {
    // localStorage fallback: a unique key value guarantees the storage event fires.
    try {
      window.localStorage.setItem(`${channelName(courseId)}-${Date.now()}-${Math.random()}`, JSON.stringify(payload));
    } catch (err) { console.warn('[statusBus] localStorage fallback failed', err); }
  }
  // Same-tab listeners always receive the event too.
  _listeners.forEach(fn => {
    try { fn(payload); } catch (err) { console.error('[statusBus] listener error', err); }
  });
}

export function closeStatusBus() {
  if (_channel) {
    try { _channel.close(); } catch { /* ignore */ }
    _channel = null;
  }
  if (_storageHandler && typeof window !== 'undefined') {
    window.removeEventListener('storage', _storageHandler);
    _storageHandler = null;
  }
  _listeners.clear();
  _stopPolling();
}
