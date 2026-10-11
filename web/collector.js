(() => {
  'use strict';
  if (window.__musicJournalCollector) return;
  window.__musicJournalCollector = true;
  const nativeFetch = window.fetch.bind(window);
  const storageKey = 'music-journal.pending.v1:' + (window.__journalAccount || 'unassigned');
  let queue = [];
  let sending = false;
  let status = '连接中';
  try { queue = JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch { queue = []; }
  if (!Array.isArray(queue)) queue = [];
  const persist = () => { try { localStorage.setItem(storageKey, JSON.stringify(queue)); } catch {} };
  const add = async body => {
    try {
      if (body instanceof Blob) body = await body.text();
      if (body instanceof ArrayBuffer) body = new TextDecoder().decode(body);
      const data = typeof body === 'string' ? JSON.parse(body) : body;
      const events = Array.isArray(data) ? data : data?.events || (data?.eventType ? [data] : []);
      if (!Array.isArray(events)) return;
      for (const event of events) {
        if (event?.eventType !== 'track_play' || !event?.payload?.trackGUID || !event?.occurredAt) continue;
        const key = `${event.payload.trackGUID}:${event.occurredAt}`;
        if (!queue.some(item => item.key === key)) queue.push({key, event});
      }
      persist();
      flush();
    } catch { status = '事件待确认'; updateBadge(); }
  };
  const endpoint = value => {
    try { return new URL(typeof value === 'string' ? value : value.url, location.href).pathname.endsWith('/event/report'); }
    catch { return false; }
  };
  const succeeded = async response => {
    if (!response.ok) return false;
    try { return (await response.clone().json()).code === 0; } catch { return false; }
  };
  window.fetch = async (input, options) => {
    const report = endpoint(input);
    const copy = report && input instanceof Request && !options?.body ? input.clone().text() : null;
    const response = await nativeFetch(input, options);
    if (report && await succeeded(response)) add(options?.body ?? (copy ? await copy : null));
    return response;
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this.__journalReport = endpoint(String(url));
    return open.call(this, method, url, ...args);
  };
  XMLHttpRequest.prototype.send = function(body) {
    if (this.__journalReport) this.addEventListener('load', () => {
      try {
        const response = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
        if (this.status >= 200 && this.status < 300 && response?.code === 0) add(body);
      } catch {}
    }, {once: true});
    return send.call(this, body);
  };
  const flush = async () => {
    if (sending || !queue.length) { updateBadge(); return; }
    sending = true;
    const batch = queue.slice(0, 100);
    try {
      const response = await nativeFetch('/api/ingest', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...(window.__journalAccount ? {'X-Journal-Account': window.__journalAccount} : {})}, body: JSON.stringify({events: batch.map(item => item.event)})});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error('pending');
      const sent = new Set(batch.map(item => item.key));
      queue = queue.filter(item => !sent.has(item.key));
      persist();
      status = '正在记录';
    } catch { status = '离线待重试'; }
    finally { sending = false; updateBadge(); }
  };
  let badge;
  const updateBadge = () => {
    if (!badge) return;
    badge.querySelector('span').textContent = `音迹 · ${queue.length ? `待同步 ${queue.length} 条` : status}`;
  };
  const mount = () => {
    badge = document.createElement('div');
    badge.setAttribute('style', 'position:fixed;right:18px;top:14px;z-index:2147483647;display:flex;align-items:center;gap:12px;padding:10px 14px;border:1px solid #ffffff24;border-radius:8px;background:#111e;color:#fff;font:12px/1.4 sans-serif;backdrop-filter:blur(12px);box-shadow:0 3px 20px #0003;');
    const link = document.createElement('a');
    link.href = '/'; link.textContent = '返回报告'; link.style.cssText = 'color:#ff5266;text-decoration:none;font-weight:700;';
    const text = document.createElement('span');
    badge.append(text, link);
    document.body.append(badge);
    status = '正在记录';
    updateBadge(); flush();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once: true}); else mount();
  window.addEventListener('online', flush);
  setInterval(flush, 10000);
})();
