'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const PERIODS = {
    week: { name: '周', title: '周报告', edition: 'WEEKLY', story: '这一周的音乐生活' },
    month: { name: '月', title: '月报告', edition: 'MONTHLY', story: '这个月的音乐生活' },
    year: { name: '年', title: '年报告', edition: 'YEARLY', story: '这一年的音乐生活' }
  };
  const state = {
    view: 'report', period: 'week', offset: 0, report: null, status: null,
    reportRequest: 0, recordsRequest: 0, reportController: null, recordsController: null,
    records: { page: 1, size: 25, total: 0, q: '', period: 'report', start: null, end: null },
    reportFailed: false, recordsFailed: false, statusFailed: false, refreshing: false,
    connectBusy: false, disconnectBusy: false, exportBusy: false, importBusy: false,
    toastTimer: null, lastFocus: null, statusSignature: ''
  };
  const icon = '<svg viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M13 28V12l17-4v17M13 12l17-4M13 17l17-4" stroke="currentColor" stroke-width="1.5"/><ellipse cx="9" cy="29" rx="4" ry="3" stroke="currentColor" stroke-width="1.5"/><ellipse cx="26" cy="26" rx="4" ry="3" stroke="currentColor" stroke-width="1.5"/></svg>';
  const e = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const count = value => Math.max(0, Number.isFinite(Number(value)) ? Number(value) : 0);
  const number = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(count(value));
  const text = (selector, value) => { $(selector).textContent = value; };
  const connected = () => Boolean(state.status?.connected && !state.status?.needs_login);
  const epoch = value => {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value))) {
      const n = Number(value);
      return Number.isFinite(n) ? (n > 0 && n < 1e11 ? n * 1000 : n) : null;
    }
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  };
  const dateFormat = (value, options = {}, timezone) => {
    const n = epoch(value);
    if (n === null) return '—';
    try { return new Intl.DateTimeFormat('zh-CN', { ...options, ...(timezone ? { timeZone: timezone } : {}) }).format(n); }
    catch { return new Intl.DateTimeFormat('zh-CN', options).format(n); }
  };
  const localDate = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const utcDate = date => date.toISOString().slice(0, 10);
  const shortDuration = ms => {
    const seconds = Math.floor(count(ms) / 1000);
    if (!seconds) return '—';
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  const longDuration = ms => {
    const minutes = Math.floor(count(ms) / 60000);
    const hours = Math.floor(minutes / 60);
    if (hours) return `${number(hours)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}`;
    if (minutes) return `${minutes} 分钟`;
    return count(ms) ? '不足 1 分钟' : '0 分钟';
  };

  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }
  // Session fallback for browsers/webviews that drop cookies: the login
  // response hands the token to the page and every request echoes it back.
  // localStorage first; when a webview disables it, a JS-readable cookie keeps
  // the token across page loads (the server's HttpOnly cookie is unaffected).
  const SESSION_KEY = 'yinji_session';
  const readTokenCookie = () => {
    const match = document.cookie.match(/(?:^|;\s*)yinji_session=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
  };
  const storedSession = () => {
    try { const value = window.localStorage.getItem(SESSION_KEY); if (value) return value; } catch { /* storage unavailable */ }
    return readTokenCookie();
  };
  const setStoredSession = token => {
    try {
      if (token) window.localStorage.setItem(SESSION_KEY, token);
      else window.localStorage.removeItem(SESSION_KEY);
    } catch { /* storage unavailable */ }
    try {
      document.cookie = token
        ? `${SESSION_KEY}=${encodeURIComponent(token)}; Path=/; Max-Age=2592000; SameSite=Strict`
        : `${SESSION_KEY}=; Path=/; Max-Age=0; SameSite=Strict`;
    } catch { /* cookie writes unavailable */ }
  };
  const sessionHeader = () => { const token = storedSession(); return token ? { 'X-Journal-Session': token } : {}; };
  // Album art is loaded by <img>, which cannot send the token header, so the
  // token rides in the URL instead.
  const coverSrc = id => {
    const token = storedSession();
    return `/api/covers/${encodeURIComponent(String(id))}${token ? `?s=${encodeURIComponent(token)}` : ''}`;
  };
  async function api(path, options = {}) {
    const controller = new AbortController();
    const outer = options.signal;
    const abort = () => controller.abort();
    if (outer?.aborted) controller.abort();
    outer?.addEventListener('abort', abort, { once: true });
    const timeout = window.setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {
        ...options, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...sessionHeader(), ...options.headers }
      });
      let data;
      try { data = await response.json(); }
      catch { throw new ApiError(response.ok ? '服务器返回了无法识别的数据。' : `请求失败（HTTP ${response.status}）。`, response.status); }
      if (!response.ok || data?.ok === false || (data?.code !== undefined && ![0, '0', 200, '200'].includes(data.code))) {
        const message = data?.error?.message || (typeof data?.error === 'string' ? data.error : '') || data?.message || data?.msg || data?.detail || (data?.code ? `飞牛拒绝了请求（业务代码 ${data.code}）。` : `请求失败（HTTP ${response.status}）。`);
        throw new ApiError(String(message), response.status);
      }
      return data;
    } catch (error) {
      if (outer?.aborted) throw error;
      if (error.name === 'AbortError') throw new ApiError('请求超时，请检查服务器连接后重试。');
      if (error instanceof ApiError) throw error;
      throw new ApiError('无法连接音迹服务，请检查网络或服务是否在线。');
    } finally {
      clearTimeout(timeout);
      outer?.removeEventListener('abort', abort);
    }
  }

  function showToast(message) {
    clearTimeout(state.toastTimer);
    text('#toast', message);
    $('#toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4200);
  }
  function showError(message) {
    text('#global-error-text', message);
    $('#global-error').hidden = false;
  }
  function hideError() { $('#global-error').hidden = true; }
  function emptyState(title, description, action = false, error = false) {
    return `<div class="empty-state">${icon.replace('<svg ', '<svg class="empty-icon" ')}<h3>${e(title)}</h3><p>${e(description)}</p>${action ? `<button type="button" class="button ${error ? 'button-outline' : 'button-dark'} button-small" ${error ? 'data-retry' : 'data-connect'}>${error ? '重新读取' : '连接飞牛音乐'} <span aria-hidden="true">↗</span></button>` : ''}</div>`;
  }
  function fallbackCover() { return `<span class="cover-placeholder" aria-hidden="true">${icon}</span>`; }
  function cover(coverId, className = '') {
    if (coverId === null || coverId === undefined || coverId === '') return `<span class="${className}">${fallbackCover()}</span>`;
    return `<span class="${className}"><img src="${coverSrc(coverId)}" loading="lazy" alt="" referrerpolicy="same-origin"></span>`;
  }
  // Broken or expired covers remain honest, decorative placeholders, not stock art.
  document.addEventListener('error', event => {
    if (event.target instanceof HTMLImageElement && event.target.closest('.ranking-cover,.record-cover,.discovery-image')) {
      event.target.parentElement.innerHTML = fallbackCover();
    }
  }, true);

  function localRange() {
    const now = new Date();
    let start;
    let end;
    if (state.period === 'year') {
      start = new Date(now.getFullYear() - state.offset, 0, 1);
      end = new Date(start.getFullYear() + 1, 0, 1);
    } else if (state.period === 'month') {
      start = new Date(now.getFullYear(), now.getMonth() - state.offset, 1);
      end = new Date(start.getFullYear(), start.getMonth() + 1, 1);
    } else {
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      start.setDate(start.getDate() - ((start.getDay() + 6) % 7) - state.offset * 7);
      end = new Date(start); end.setDate(end.getDate() + 7);
    }
    return { start: start.getTime(), end: end.getTime() };
  }
  function rangeLabel(report = state.report) {
    if (report?.label) return String(report.label);
    const range = localRange();
    if (state.period === 'year') return `${new Date(range.start).getFullYear()} 年`;
    if (state.period === 'month') return dateFormat(range.start, { year: 'numeric', month: 'long' });
    return `${dateFormat(range.start, { month: '2-digit', day: '2-digit' })} — ${dateFormat(range.end - 1, { month: '2-digit', day: '2-digit' })}`;
  }
  function updatePeriodUI() {
    $$('[data-period]').forEach(button => {
      const selected = button.dataset.period === state.period;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    const name = PERIODS[state.period].name;
    document.body.classList.toggle('report-theme', state.view !== 'records');
    document.body.classList.toggle('year-theme', state.period === 'year' && state.view !== 'records');
    document.body.dataset.period = state.period;
    $('#older-report').setAttribute('aria-label', `查看上一${name}`);
    $('#newer-report').setAttribute('aria-label', `查看下一${name}`);
    $('#newer-report').disabled = state.offset === 0;
    $('#current-report').hidden = state.offset === 0;
    text('#report-date', rangeLabel());
    text('#cover-edition', PERIODS[state.period].edition);
    text('#cover-period', state.offset ? `那一${name}的音乐生活` : PERIODS[state.period].story);
    text('#cover-range', rangeLabel());
    text('#cover-volume', state.report ? dateFormat(state.report.start, { year: 'numeric' }, state.report.timezone).replace('年', '') : '—');
    $('#calendar-section').hidden = state.period !== 'year';
    text('#change-description', `与上一${name}相比`);
    text('#timeline-description', state.period === 'year' ? '按月记录播放开始次数，与上一年对照。' : `按天记录播放开始次数，与上一${name}对照。`);
    document.title = `${state.view === 'records' ? '播放记录' : PERIODS[state.period].title} · 音迹`;
  }

  function renderStatus() {
    const status = state.status;
    const isConnected = connected();
    $('#status-dot').className = `status-dot${isConnected ? ' connected' : status?.error || state.statusFailed ? ' error' : ''}`;
    const name = isConnected ? status.user?.name || '我的音乐' : '你的音乐，音迹可循';
    text('#header-user', isConnected ? '连接设置' : '连接飞牛账号');
    text('#header-avatar', isConnected ? (name.slice(0, 1).toUpperCase() || '♫') : '♫');
    $('#connected-account').hidden = !isConnected;
    $('#change-account').hidden = !isConnected;
    $('#restore-share').hidden = isConnected;
    $('#connection-form').hidden = isConnected;
    if (isConnected) {
      text('#account-name', status.user?.name || '飞牛音乐账号');
      text('#account-server', status.server || '已连接服务器');
      text('#account-sync', status.last_sync ? `上次同步 ${dateFormat(status.last_sync, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}` : '还未进行曲库同步');
    }
  }
  function statusFingerprint(status) {
    return JSON.stringify([status.connected, status.needs_login, status.user?.name, status.last_sync, status.collection?.exact, status.collection?.estimated]);
  }
  async function loadStatus() {
    const status = await api('/api/status');
    if (!status || typeof status.connected !== 'boolean') throw new ApiError('连接状态数据不完整，请重试。');
    state.status = status;
    state.statusFailed = false;
    state.statusSignature = statusFingerprint(status);
    renderStatus();
    return status;
  }
  function authExpired() {
    setStoredSession('');
    state.status = { connected: false, needs_login: true, notice: '请登录飞牛账号以查看私人收听记录' };
    state.reportController?.abort();
    state.recordsController?.abort();
    state.report = null;
    state.records.total = 0;
    state.reportFailed = false;
    state.recordsFailed = false;
    renderStatus();
    renderEmptyReport('login');
    renderEmptyRecords('login');
    hideError();
    showToast('会话已过期，请重新连接飞牛音乐。');
  }

  function renderLoadingCharts() {
    const heights = ['38%', '63%', '46%', '82%', '55%', '35%', '72%'];
    $('#timeline-chart').innerHTML = `<div class="chart-skeleton" aria-hidden="true">${heights.map(height => `<span class="skeleton" style="--height:${height}"></span>`).join('')}</div><p class="loading-label">正在读取真实播放记录…</p>`;
    $('#hourly-chart').innerHTML = '<div class="skeleton" style="height:140px" aria-hidden="true"></div>';
    ['artists', 'albums', 'tracks'].forEach(type => {
      $(`#top-${type}`).innerHTML = Array.from({ length: 3 }, () => '<div class="ranking-skeleton" aria-hidden="true"><span class="skeleton"></span><span class="skeleton"></span></div>').join('');
    });
  }
  function renderEmptyReport(kind = 'empty', message = '') {
    const isLogin = kind === 'login';
    const isError = kind === 'error';
    $('#report-loading').hidden = true;
    $('#report-content').hidden = true;
    $('#report-empty').hidden = false;
    text('#report-empty-title', isError ? '暂时无法读取收听报告' : '您没有收听任何音乐');
    text('#report-empty-description', isError ? message || '报告暂时不可用，请检查连接后重试。' : isLogin ? '还没有音乐记录，因此没有收听报告。马上开始音乐记录！' : '您好像还没有进行音乐记录，因此没有收听报告。下次不要错过，马上开始音乐记录！');
    const action = $('#report-empty-action');
    delete action.dataset.connect; delete action.dataset.retry;
    action.removeAttribute('data-play');
    if (isError) action.dataset.retry = '';
    else if (isLogin) action.dataset.connect = '';
    else action.dataset.play = '';
    action.textContent = isError ? '重新读取报告' : '追踪我的音乐';
    text('#report-empty-help', isError ? '不展示旧数据，也不伪造播放记录。' : isLogin ? '连接飞牛音乐，正常播放即可开始记录。' : '连接只同步音乐信息，不补造历史。连接后正常播放即可开始记录。');
    const title = isLogin ? '你的音乐，值得被记住' : isError ? '这份报告暂时读不到' : '音乐的下一页，等待你来写';
    const description = isLogin ? '连接飞牛账号后正常播放即可自动记录。你的私人记录仅登录后可见。' : isError ? message || '无法获取报告，请检查服务连接后重新读取。' : '本期没有采集到播放。正常播放音乐，下一次播放就会成为第一条音迹。';
    state.report = null;
    $('#report-content').classList.remove('is-loading');
    $('#report-content').setAttribute('aria-busy', 'false');
    updatePeriodUI();
    text('#cover-plays', isLogin || isError ? '—' : '0');
    text('#cover-footnote', isLogin ? '先连接，再让下一次播放留下痕迹' : isError ? '数据暂不可用 · 不展示旧数据' : '真实播放开始事件 · 不补造历史');
    text('#summary-title', isLogin ? '你的音乐故事，从下一次播放开始。' : isError ? '等连接恢复，再继续这段故事。' : '还没有播放记录，但故事才刚开始。');
    text('#summary-description', isLogin ? '连接飞牛音乐，只在登录后查看你的私人听歌报告。首次同步曲库不等于补录播放历史。' : isError ? description : '空白不是遗漏的数字。只有实际采集到的播放开始事件，才会成为报告中的一次播放。');
    text('#report-state', isLogin ? '等待连接' : isError ? '读取失败' : '暂无播放');
    text('#change-value', '—');
    text('#change-description', '有两期记录后，变化才有意义');
    ['duration', 'artists', 'albums', 'tracks', 'days'].forEach(key => text(`#metric-${key}`, isLogin || isError ? '—' : key === 'duration' ? '0 分钟' : '0'));
    text('#discovery-artists', isLogin || isError ? '—' : '0');
    text('#discovery-tracks', isLogin || isError ? '—' : '0');
    $('#story-insights').innerHTML = '';
    $('#discovery-artist').innerHTML = '<p class="empty-discovery">A new sound awaits.<small>新声音，等你来发现。</small></p>';
    $('#timeline-chart').innerHTML = emptyState(title, description, isLogin || isError, isError);
    $('#hourly-chart').innerHTML = emptyState('还没有听歌时间分布', isLogin ? '连接后，实际采集到的播放时间将在这里汇聚。' : isError ? '报告恢复后即可查看。' : '先留下一次真实播放，音乐时钟才会开始走动。');
    ['artists', 'albums', 'tracks'].forEach(type => {
      $(`#top-${type}`).innerHTML = emptyState({ artists: '等待你喜欢的声音', albums: '等待一张相伴的专辑', tracks: '等待第一首被记住的歌' }[type], isLogin ? '登录并播放后，这里会出现你的真实排名。' : isError ? '数据暂时不可用，不展示旧的排名。' : '暂无记录，不使用示例榜单。');
    });
    $('#calendar-chart').innerHTML = emptyState('一年，一页还未写完的音乐日历', isLogin ? '连接账号后查看有声音的日子。' : isError ? '报告恢复后即可查看。' : '有播放的日子，会在这里亮起。');
    text('#hourly-description', '一天 24 小时，看看你的听歌习惯。');
    text('#timeline-caption', '每一根柱子，都是一段有声音的时间。');
    text('#timezone-label', '时间以服务器报告时区为准');
  }

  async function loadReport({ quiet = false } = {}) {
    if (!connected()) { renderEmptyReport('login'); return; }
    state.reportController?.abort();
    const controller = new AbortController();
    state.reportController = controller;
    const request = ++state.reportRequest;
    state.reportFailed = false;
    if (!quiet) {
      state.report = null;
      renderEmptyReport('empty');
      renderLoadingCharts();
      $('#report-empty').hidden = true;
      $('#report-content').hidden = true;
      $('#report-loading').hidden = false;
      $('#report-content').classList.add('is-loading');
      $('#report-content').setAttribute('aria-busy', 'true');
      text('#report-state', '读取中');
      ['#cover-plays', '#metric-duration', '#metric-artists', '#metric-albums', '#metric-tracks', '#metric-days', '#discovery-artists', '#discovery-tracks'].forEach(selector => text(selector, '—'));
      text('#summary-title', '正在翻开你的音乐报告。');
      text('#summary-description', '只读取实际采集的数据，请稍等。');
    }
    try {
      const report = await api(`/api/report?period=${state.period}&offset=${state.offset}`, { signal: controller.signal });
      if (request !== state.reportRequest || controller.signal.aborted) return;
      if (!report || !report.totals || !Array.isArray(report.timeline)) throw new ApiError('报告数据不完整，请重新读取。');
      state.report = report;
      renderReport(report);
    } catch (error) {
      if (request !== state.reportRequest || controller.signal.aborted) return;
      if (error.status === 401) { authExpired(); return; }
      state.reportFailed = true;
      renderEmptyReport('error', error.message);
      showError(`听歌报告：${error.message}`);
    } finally {
      if (request === state.reportRequest) {
        $('#report-content').classList.remove('is-loading');
        $('#report-content').setAttribute('aria-busy', 'false');
      }
    }
  }

  function renderReport(report) {
    updatePeriodUI();
    const total = report.totals;
    const plays = count(total.plays);
    $('#report-loading').hidden = true;
    $('#report-content').hidden = plays === 0;
    $('#report-empty').hidden = plays > 0;
    if (plays === 0) {
      text('#report-empty-title', '您没有收听任何音乐');
      text('#report-empty-description', '您好像还没有进行音乐记录，因此没有收听报告。下次不要错过，马上开始音乐记录！');
      text('#report-empty-help', '没有真实播放开始事件时，报告保持空白。播放由轮询探测记录，不补造无日期的旧历史。');
      $('#report-empty-action').textContent = '追踪我的音乐';
      delete $('#report-empty-action').dataset.connect;
      delete $('#report-empty-action').dataset.retry;
      $('#report-empty-action').dataset.play = '';
    }
    const representativeCover = report.tops?.artists?.[0]?.cover_id || report.tops?.albums?.[0]?.cover_id;
    document.body.style.setProperty('--year-cover', representativeCover ? `url("${coverSrc(representativeCover)}")` : 'none');
    const previous = count(report.previous?.plays);
    const period = PERIODS[state.period];
    text('#cover-plays', number(plays));
    text('#cover-footnote', '播放次数 = 真实播放开始事件 · 不等同于 Last.fm 有效 Scrobble · 时长不代表实际听完');
    ['artists', 'albums', 'tracks'].forEach(key => {
      const before = count(report.previous?.[key]);
      const current = count(total[key]);
      const delta = before ? (current - before) / before * 100 : null;
      const label = delta === null ? '上期无可比记录' : `${delta > 0 ? '↑' : delta < 0 ? '↓' : '→'} ${Math.abs(delta).toFixed(0)}% 与上期相比`;
      text(`#metric-${key}-change`, label);
      text(`#rank-${key}-count`, number(current));
      text(`#rank-${key}-change`, label);
    });
    text('#report-state', plays === 0 ? '暂无播放' : report.complete ? '已结束' : '本期进行中');
    text('#metric-duration', longDuration(total.estimated_duration_ms));
    ['artists', 'albums', 'tracks'].forEach(key => text(`#metric-${key}`, number(total[key])));
    $('#metric-days').innerHTML = `${number(total.active_days)}<span class="metric-unit">天</span>`;
    text('#summary-title', plays ? `在${rangeLabel(report)}，音乐为你留下了 ${number(plays)} 次回响。` : '这一期还很安静，下一首歌会留下痕迹。');
    text('#summary-description', plays ? `你与 ${number(total.artists)} 位歌手、${number(total.tracks)} 首曲目相遇。曲目时长合计约 ${longDuration(total.estimated_duration_ms)}，不代表实际收听或听完时长。` : '这不是示例报告：本期确实尚未采集到播放。首次同步仅有曲库元数据；正常播放音乐即可留下真正的播放事件。');
    const change = report.change_percent;
    if (change !== null && change !== undefined && Number.isFinite(Number(change))) {
      const value = Number(change);
      text('#change-value', `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%`);
      $('#change-value').classList.toggle('negative', value < 0);
      text('#change-description', `比上一${period.name}${value > 0 ? '多听了一些' : value < 0 ? '更安静一些' : '一样多的播放'}${!report.complete ? ' · 本期未结束' : ''}`);
    } else {
      text('#change-value', '—');
      $('#change-value').classList.remove('negative');
      text('#change-description', previous === 0 ? `上一${period.name}无播放，暂无可比增幅` : `暂无可比增幅${!report.complete ? ' · 本期未结束' : ''}`);
    }
    const insights = Array.isArray(report.insights) ? report.insights.slice(0, 2) : [];
    $('#story-insights').innerHTML = insights.map(item => `<div class="insight"><span class="insight-label">${e(item.title)}</span><strong>${e(item.value ?? '—')}</strong>${item.detail ? `<small>${e(item.detail)}</small>` : ''}</div>`).join('');
    text('#timezone-label', report.timezone ? `报告时区 · ${report.timezone}` : '时间以服务器报告时区为准');
    text('#timeline-caption', report.complete ? `上一${period.name} ${number(previous)} 次 / 本期 ${number(plays)} 次播放开始` : `上一${period.name} ${number(previous)} 次 / 本期 ${number(plays)} 次 · 当前周期未结束`);
    renderTimeline(report.timeline, report);
    $('#year-summary-chart').innerHTML = state.period === 'year' ? $('#timeline-chart').innerHTML : '';
    $('#year-summary-chart').hidden = state.period !== 'year';
    $('#year-playback').hidden = state.period !== 'year';
    $('#year-modules').hidden = state.period !== 'year';
    ['artists', 'albums', 'tracks'].forEach(type => renderRanking(type, report.tops?.[type] || []));
    renderYearModules(report);
    const discovery = report.discovery || {};
    text('#discovery-artists', number(discovery.artists));
    text('#discovery-tracks', number(discovery.tracks));
    const artist = discovery.top_artist;
    $('#discovery-artist').innerHTML = artist ? `${cover(artist.cover_id, 'discovery-image')}<div class="feature-copy"><span>本期新发现</span><strong title="${e(artist.name)}">${e(artist.name || '未知歌手')}</strong><span>${number(artist.plays)} 次播放</span></div>` : '<p class="empty-discovery">A new sound awaits.<small>这一期还没有发现新的歌手。</small></p>';
    renderHourly(Array.isArray(report.hourly) ? report.hourly : []);
    if (state.period === 'year') renderCalendar(Array.isArray(report.calendar) ? report.calendar : [], report);
    replayCharts($('#report-content'));
    watchReveal($('#report-content'));
  }

  function renderRanking(type, entries) {
    const container = $(`#top-${type}`);
    const items = Array.isArray(entries) ? entries.slice(0, 5) : [];
    if (!items.length) {
      container.innerHTML = emptyState({ artists: '等待你喜欢的声音', albums: '等待一张相伴的专辑', tracks: '等待第一首被记住的歌' }[type], '本期暂无采集排名，不使用示例数据。');
      return;
    }
    container.innerHTML = `<ol class="ranking-list" aria-label="${{ artists: '歌手', albums: '专辑', tracks: '曲目' }[type]}排名">${items.map((item, index) => {
      const name = item.name || item.title || (type === 'artists' ? item.artist : null) || '未知名称';
      const subtitle = type !== 'artists' ? item.artist || '未知歌手' : '';
      return `<li class="ranking-row"><span class="ranking-number" aria-label="第 ${index + 1} 名">${String(index + 1).padStart(2, '0')}</span>${cover(item.cover_id, `ranking-cover${type === 'artists' ? ' artist-cover' : ''}`)}<div class="ranking-info"><span class="ranking-name" title="${e(name)}">${e(name)}</span>${subtitle ? `<span class="ranking-sub" title="${e(subtitle)}">${e(subtitle)}</span>` : ''}</div><span class="ranking-plays">${number(item.plays)}<small>次播放</small></span></li>`;
    }).join('')}</ol>`;
  }
  function niceMax(value) {
    const maximum = Math.max(1, value);
    const magnitude = 10 ** Math.floor(Math.log10(maximum));
    const normalized = maximum / magnitude;
    return (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  }
  function renderTimeline(items, report) {
    const series = items.filter(item => item && typeof item === 'object');
    if (!series.length || !series.some(item => count(item.plays) || count(item.previous))) {
      $('#timeline-chart').innerHTML = emptyState('这一期，等待第一段旋律', '还没有采集到本期或上一期播放。正常播放音乐，留下你的音乐足迹。');
      return;
    }
    const width = 1000, height = 230, left = 38, right = 10, top = 15, bottom = 32;
    const plotHeight = height - top - bottom;
    const maximum = niceMax(Math.max(...series.map(item => Math.max(count(item.plays), count(item.previous)))));
    const step = (width - left - right) / series.length;
    const barWidth = Math.min(27, step * .29);
    const tickSkip = series.length > 16 ? Math.ceil(series.length / 9) : 1;
    const annual = state.period === 'year';
    let svg = `<svg class="${annual ? '' : 'chart-wipe'}" viewBox="0 0 ${width} ${height}" role="img" aria-label="本期与上一期播放开始次数${annual ? '柱状' : '趋势'}图。${e(report.label || rangeLabel(report))}。可通过键盘聚焦查看数据。"><title>本期与上一期播放次数</title>`;
    const points = series.map((item, index) => [left + step * (index + .5), top + plotHeight - count(item.plays) / maximum * plotHeight]);
    const previousPoints = series.map((item, index) => [left + step * (index + .5), top + plotHeight - count(item.previous) / maximum * plotHeight]);
    for (let i = 0; i <= 4; i++) {
      const y = top + plotHeight - i * plotHeight / 4;
      const value = maximum * i / 4;
      svg += `<line class="chart-grid" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text class="chart-text y-label" x="${left - 10}" y="${y + 3}" text-anchor="end">${e(Number.isInteger(value) ? number(value) : '')}</text>`;
    }
    if (!annual) {
      const path = points.map(point => point.join(',')).join(' ');
      svg += `<polygon class="area-current" points="${points[0][0]},${top + plotHeight} ${path} ${points[points.length - 1][0]},${top + plotHeight}"/><polyline class="line-previous" points="${previousPoints.map(point => point.join(',')).join(' ')}"/><polyline class="line-current" points="${path}"/>`;
    }
    series.forEach((item, index) => {
      const middle = left + step * (index + .5);
      const current = count(item.plays), previous = count(item.previous);
      const label = item.label || (state.period === 'year' ? `${index + 1}月` : dateFormat(item.date, { month: 'numeric', day: 'numeric' }, report.timezone));
      const description = `${label}：本期 ${number(current)} 次，上一期 ${number(previous)} 次`;
      const previousHeight = previous / maximum * plotHeight;
      const currentHeight = current / maximum * plotHeight;
      if (annual) svg += `<g class="bar-grow" style="transition-delay:${index * 40}ms" tabindex="0" role="img" aria-label="${e(description)}"><title>${e(description)}</title><rect class="bar-previous" x="${middle - barWidth - 2}" y="${top + plotHeight - previousHeight}" width="${barWidth}" height="${previousHeight}" rx="1"/><rect class="bar-current" x="${middle + 2}" y="${top + plotHeight - currentHeight}" width="${barWidth}" height="${currentHeight}" rx="1"/></g>`;
      else svg += `<g tabindex="0" role="img" aria-label="${e(description)}"><title>${e(description)}</title><circle class="chart-point" cx="${middle}" cy="${top + plotHeight - currentHeight}" r="3"/><circle class="chart-hit" cx="${middle}" cy="${top + plotHeight - currentHeight}" r="11" fill="transparent"/></g>`;
      if (index % tickSkip === 0 || index === series.length - 1) svg += `<text class="chart-text" x="${middle}" y="${height - 10}" text-anchor="middle">${e(label)}</text>`;
    });
    $('#timeline-chart').innerHTML = `${svg}</svg>`;
  }
  function renderHourly(raw) {
    const values = Array.from({ length: 24 }, (_, index) => count(raw[index]));
    const peak = Math.max(...values);
    if (!peak) {
      $('#hourly-chart').innerHTML = emptyState('音乐时钟还没有开始走动', '有真实播放时间后，这里会显示一天中的听歌分布。');
      text('#hourly-description', '一天 24 小时，看看你的听歌习惯。');
      return;
    }
    const peakHours = values.map((value, index) => value === peak ? index : null).filter(value => value !== null);
    text('#hourly-description', `你最常在 ${peakHours.slice(0, 3).map(hour => `${String(hour).padStart(2, '0')}:00`).join('、')}${peakHours.length > 3 ? ' 等时段' : ''} 与音乐相遇。`);
    const cx = 150, cy = 147, inner = 34, outer = 105;
    let radial = '<svg class="radial-clock" viewBox="0 0 300 300" role="img" aria-label="24 小时收听时钟，每根射线代表一小时的真实播放次数"><title>收听时钟</title>';
    [55, 80, 105].forEach(radius => { radial += `<circle class="clock-guide" cx="${cx}" cy="${cy}" r="${radius}"/>`; });
    const polar = (radius, angle) => [cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius];
    values.forEach((value, hour) => {
      const angle = hour / 24 * Math.PI * 2 - Math.PI / 2;
      const [x1, y1] = polar(inner, angle);
      const [x2, y2] = polar(inner + value / peak * (outer - inner), angle);
      const description = `${String(hour).padStart(2, '0')}:00：${number(value)} 次真实播放开始`;
      radial += `<line class="clock-ray${value === peak ? ' peak' : ''} ray-draw" pathLength="1" style="transition-delay:${hour * 30}ms" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" tabindex="0" role="img" aria-label="${e(description)}"><title>${e(description)}</title></line>`;
      if (hour % 3 === 0) {
        const [x, y] = polar(125, angle);
        radial += `<text class="clock-label" x="${x}" y="${y + 4}" text-anchor="middle">${String(hour).padStart(2, '0')}:00</text>`;
      }
    });
    radial += `<text class="clock-center" x="${cx}" y="${cy - 1}" text-anchor="middle">${String(peakHours[0]).padStart(2, '0')}:00</text><text class="clock-subtitle" x="${cx}" y="${cy + 16}" text-anchor="middle">收听高峰</text></svg>`;
    $('#hourly-chart').innerHTML = radial;
  }
  function renderYearModules(report) {
    if (state.period !== 'year') return;
    const discovery = report.discovery || {};
    $('#year-discoveries').innerHTML = ['artists', 'albums', 'tracks'].map(type => {
      const name = { artists: '歌手', albums: '专辑', tracks: '单曲' }[type];
      const total = count(report.totals[type]);
      const discovered = count(discovery[type]);
      const top = discovery[{ artists: 'top_artist', albums: 'top_album', tracks: 'top_track' }[type]];
      const pct = total ? Math.min(100, discovered / total * 100) : 0;
      return `<div class="discovery-card"><span>新${name}</span><strong>${pct.toFixed(0)}<small>%</small></strong><p>${number(discovered)} 位 / 首首次出现在记录中</p>${top ? `<div class="new-feature">${cover(top.cover_id, 'record-cover')}<div><strong>${e(top.name || top.title || '未知名称')}</strong><span>${number(top.plays)} 次播放</span></div></div>` : '<p class="data-unavailable">暂无新的采集发现</p>'}</div>`;
    }).join('');
    const first = report.first_play;
    $('#first-play').innerHTML = first ? `<div class="first-play-track">${cover(first.cover_id, 'record-cover')}<div><strong>${e(first.title || '未知曲目')}</strong><span>${e(first.artist || '未知歌手')} · ${e(dateFormat(first.played_at, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }, report.timezone))}</span></div></div>` : '<p class="data-unavailable">暂无带真实播放时间的首条记录。</p>';
    const weekdays = Array.isArray(report.weekday) ? report.weekday : [];
    if (weekdays.some(item => count(item.plays) || count(item.previous))) {
      const max = Math.max(1, ...weekdays.map(item => Math.max(count(item.plays), count(item.previous))));
      const labels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
      $('#weekday-chart').innerHTML = `<div class="weekday-bars" role="img" aria-label="周一至周日的真实播放次数">${weekdays.map((item, index) => `<div class="weekday-item"><div class="weekday-pair"><i class="previous bar-grow" style="height:${count(item.previous) / max * 100}%;transition-delay:${index * 70}ms" title="上期 ${number(item.previous)} 次"></i><i class="current bar-grow" style="height:${count(item.plays) / max * 100}%;transition-delay:${index * 70 + 50}ms" title="本期 ${number(item.plays)} 次"></i></div><span>${labels[Number(item.day) || index] || `第${index + 1}天`}</span><strong>${number(item.plays)}</strong></div>`).join('')}</div>`;
    } else $('#weekday-chart').innerHTML = emptyState('暂无每周收听分布', '有真实播放时间后，才能统计星期分布。');
    const bars = (selector, data, label, suffix = '次', digits = 0) => {
      const values = Array.isArray(data) ? data.filter(item => count(item.plays ?? item.value) > 0).slice(0, 12) : [];
      if (!values.length) { $(selector).innerHTML = `<p class="data-unavailable">${e(label)}</p>`; return; }
      const maximum = Math.max(...values.map(item => count(item.plays ?? item.value)));
      const format = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(count(value));
      $(selector).innerHTML = values.map((item, index) => `<div class="extra-bar"><span>${e(item.name || item.label || `${item.decade}年代`)}</span><div><i class="bar-wide" style="width:${count(item.plays ?? item.value) / maximum * 100}%;transition-delay:${index * 70}ms"></i></div><strong>${format(item.plays ?? item.value)}${e(suffix)}</strong></div>`).join('');
    };
    bars('#genres-chart', report.genres, '暂无真实流派标签，未知流派不进行猜测。');
    bars('#decades-chart', report.decades, '暂无可用专辑发行年份，不推测年代。');
    bars('#fingerprint-chart', report.fingerprint, '采集记录不足，暂无个人收听指纹。', '%', 1);
    const ratios = report.ratios;
    $('#ratios-chart').innerHTML = ratios && ['artists', 'albums', 'tracks'].some(key => Number.isFinite(Number(ratios[key]))) ? ['artists', 'albums', 'tracks'].map(key => {
      const value = Number(ratios[key]);
      const valid = ratios[key] !== null && Number.isFinite(value);
      const percent = valid ? Math.max(0, Math.min(100, value)) : 0;
      return `<div class="ratio-stat"><div class="ratio-ring" style="--ratio:${percent}%"><strong>${valid ? `${percent.toFixed(0)}%` : '—'}</strong></div><span>${{ artists: '歌手', albums: '专辑', tracks: '单曲' }[key]}多样性</span></div>`;
    }).join('') : '<p class="data-unavailable">记录不足，暂无音乐比例。</p>';
    const facts = Array.isArray(report.quick_facts) ? report.quick_facts : [];
    $('#quick-facts').innerHTML = facts.length ? facts.map(item => `<div class="fact"><span>${e(item.title)}</span><strong>${e(item.value ?? '—')}</strong><p>${e(item.detail || '')}</p></div>`).join('') : '<p class="data-unavailable">暂无快速事实，等待更多真实播放记录。</p>';
    renderGenreStream(report.genre_stream);
  }
  function renderGenreStream(stream) {
    const container = $('#genre-stream');
    const days = Array.isArray(stream?.days) ? stream.days : [];
    const series = Array.isArray(stream?.series) ? stream.series.slice(0, 10) : [];
    if (!days.length || !series.length || !series.some(item => item.values?.some(value => count(value)))) { container.hidden = true; container.innerHTML = ''; return; }
    container.hidden = false;
    const width = 1000, height = 210;
    const totals = days.map((_, index) => series.reduce((sum, item) => sum + count(item.values?.[index]), 0));
    const maximum = Math.max(1, ...totals);
    const baseline = days.map(() => 0);
    const colors = ['#f4d2fb', '#c29dda', '#b372c2', '#87579e', '#e2b6c5', '#ae617d', '#7056a4', '#947fba', '#e0b2e8', '#ca8fbc'];
    let svg = '<svg viewBox="0 0 1000 210" role="img" aria-label="真实流派随时间的播放分布"><title>流派收听流</title>';
    series.forEach((item, s) => {
      const lower = [], upper = [];
      days.forEach((_, index) => {
        const x = days.length === 1 ? 500 : index / (days.length - 1) * width;
        lower.push([x, height - baseline[index] / maximum * height]);
        baseline[index] += count(item.values?.[index]);
        upper.push([x, height - baseline[index] / maximum * height]);
      });
      const points = [...lower, ...upper.reverse()].map(point => point.join(',')).join(' ');
      svg += `<polygon points="${points}" fill="${colors[s]}" tabindex="0" role="img" aria-label="${e(item.name)}，${number(item.values?.reduce((sum, value) => sum + count(value), 0))} 次播放"><title>${e(item.name)}</title></polygon>`;
    });
    container.innerHTML = `${svg}</svg><div class="stream-legend">${series.map((item, index) => `<span><i style="background:${colors[index]}"></i>${e(item.name)}</span>`).join('')}</div>`;
  }
  function renderCalendar(items, report) {
    const startDate = epoch(report.start) === null ? new Date() : new Date(epoch(report.start));
    let year = startDate.getFullYear();
    const yearText = dateFormat(report.start, { year: 'numeric' }, report.timezone).match(/\d{4}/)?.[0];
    if (yearText) year = Number(yearText);
    if (!Number.isFinite(year) || year < 1900 || year > 2200) year = new Date().getFullYear();
    const values = new Map(items.map(item => [String(item.date).slice(0, 10), count(item.plays)]));
    const first = new Date(Date.UTC(year, 0, 1));
    const last = new Date(Date.UTC(year + 1, 0, 1));
    const days = Math.round((last - first) / 86400000);
    const firstDay = (first.getUTCDay() + 6) % 7;
    const columns = Math.ceil((days + firstDay) / 7);
    const size = 16, left = 32, top = 23;
    const width = left + columns * size + 4;
    const height = top + size * 7 + 6;
    const peak = Math.max(0, ...values.values());
    const palette = document.documentElement.classList.contains('theme-light')
      ? ['#ececec', '#d9c2e6', '#b78fd1', '#8e5bb0', '#5f3a7d']
      : ['#1e1d22', '#4c3b58', '#7b5b93', '#a97fc6', '#f7d7ff'];
    const active = [...values.values()].filter(value => value > 0).length;
    let svg = `<svg viewBox="0 0 ${width} ${height}" role="group" aria-label="${year} 年听歌日历，共 ${active} 天有采集记录。使用方向键移动日期，Home 或 End 跳到年初或年末。"><title>${year} 年听歌日历</title>`;
    ['一', '三', '五', '日'].forEach((label, index) => {
      const row = [0, 2, 4, 6][index];
      svg += `<text class="calendar-label" x="15" y="${top + row * size + 10}" text-anchor="middle">${label}</text>`;
    });
    for (let month = 0; month < 12; month++) {
      const index = Math.round((Date.UTC(year, month, 1) - first.getTime()) / 86400000);
      const column = Math.floor((index + firstDay) / 7);
      svg += `<text class="calendar-label" x="${left + column * size}" y="12">${month + 1}月</text>`;
    }
    for (let index = 0; index < days; index++) {
      const day = new Date(first.getTime() + index * 86400000);
      const key = utcDate(day);
      const value = values.get(key) || 0;
      const level = value && peak ? Math.max(1, Math.ceil(value / peak * 4)) : 0;
      const column = Math.floor((index + firstDay) / 7);
      const row = (index + firstDay) % 7;
      const description = `${year}年${day.getUTCMonth() + 1}月${day.getUTCDate()}日：${value ? `${number(value)} 次播放开始` : '无采集记录'}`;
      svg += `<rect class="calendar-cell" data-calendar-index="${index}" x="${left + column * size}" y="${top + row * size}" width="${size}" height="${size}" rx="2" fill="${palette[level]}" tabindex="${index === 0 ? '0' : '-1'}" role="img" aria-label="${e(description)}"><title>${e(description)}</title></rect>`;
    }
    $('#calendar-chart').innerHTML = `${svg}</svg>`;
  }
  $('#calendar-chart').addEventListener('keydown', event => {
    const cell = event.target.closest('[data-calendar-index]');
    if (!cell) return;
    const cells = $$('[data-calendar-index]', $('#calendar-chart'));
    const index = Number(cell.dataset.calendarIndex);
    const directions = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -7, ArrowRight: 7 };
    let next;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = cells.length - 1;
    else if (event.key in directions) next = Math.max(0, Math.min(cells.length - 1, index + directions[event.key]));
    else return;
    event.preventDefault();
    cell.setAttribute('tabindex', '-1');
    cells[next].setAttribute('tabindex', '0');
    cells[next].focus({ preventScroll: true });
    cells[next].scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' });
  });

  function effectiveRecordRange() {
    const choice = state.records.period;
    const now = new Date();
    if (choice === 'all') return { start: null, end: null };
    if (choice === 'report') {
      if (!state.report) return { start: null, end: null };
      return { start: epoch(state.report.start), end: epoch(state.report.end) };
    }
    if (choice === 'custom') return { start: state.records.start, end: state.records.end };
    if (choice === 'year') return { start: new Date(now.getFullYear(), 0, 1).getTime(), end: new Date(now.getFullYear() + 1, 0, 1).getTime() };
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    start.setDate(start.getDate() - (choice === 'week' ? 6 : 29));
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    return { start: start.getTime(), end: end.getTime() };
  }
  function recordParams(withPagination = true) {
    const params = new URLSearchParams();
    if (withPagination) { params.set('page', state.records.page); params.set('size', state.records.size); }
    if (state.records.q) params.set('q', state.records.q);
    const { start, end } = effectiveRecordRange();
    if (start !== null) params.set('start', start);
    if (end !== null) params.set('end', end);
    return params;
  }
  function renderRecordFilterSummary() {
    const { start, end } = effectiveRecordRange();
    let label = state.records.period === 'report' ? `报告周期 · ${rangeLabel()}` : state.records.period === 'all' ? '所有已采集记录' : `${dateFormat(start, { year: 'numeric', month: '2-digit', day: '2-digit' })} — ${dateFormat(end === null ? null : end - 1, { year: 'numeric', month: '2-digit', day: '2-digit' })}`;
    if (state.records.q) label += ` / 搜索「${state.records.q}」`;
    text('#records-filter-summary', label);
  }
  function renderEmptyRecords(kind = 'empty', message = '') {
    $('#records-body').innerHTML = '';
    const isLogin = kind === 'login';
    const isError = kind === 'error';
    const filtered = state.records.q || state.records.period !== 'all';
    $('#records-empty').hidden = false;
    $('#records-empty').innerHTML = emptyState(isLogin ? '播放档案，只为你打开' : isError ? '暂时无法读取播放记录' : filtered ? '还没有符合条件的音迹' : '每一份档案，都从第一首歌开始', isLogin ? '连接飞牛账号后查看私人记录。没有连接时，不会展示任何播放历史。' : isError ? message : filtered ? '试试其他周期或关键词；如果还未开始采集，请从本站打开飞牛播放器。' : '本工具不会用曲库伪造历史播放。从本站打开飞牛播放器，让下一次真实播放留下痕迹。', isLogin || isError, isError);
    text('#records-count', isLogin ? '连接后查看真实播放档案' : isError ? '数据暂不可用' : '共 0 条记录');
    text('#records-page', '1 / 1');
    text('#pagination-description', '每页 25 条');
    $('#records-prev').disabled = true;
    $('#records-next').disabled = true;
    $('#export-button').disabled = true;
    $('#records-results').setAttribute('aria-busy', 'false');
    $('#records-results').classList.remove('is-loading');
    renderRecordFilterSummary();
  }
  function sourceDescription(item) {
    const source = String(item.source || '').toLowerCase();
    const confidence = String(item.confidence ?? '').toLowerCase();
    if (source.includes('history') || source.includes('poll')) {
      return { label: '轮询探测', className: '', detail: '按首次发现时间估算播放时间；已与代理/浏览器精确采集去重' };
    }
    const exact = ['exact', 'high', 'confirmed'].includes(confidence) || ['gateway', 'browser_bridge', 'proxy', 'playback', 'collector', 'event', 'player'].some(value => source.includes(value));
    return exact ? { label: source === 'browser_bridge' ? '浏览器采集' : source === 'gateway' ? '代理采集' : '精确采集', className: '', detail: '实际捕获的播放开始事件；曲目时长为估算，不表示已听完' } : { label: '待确认', className: 'unknown', detail: '采集来源未明确标注；无日期旧历史不计入报告' };
  }
  function renderRecords(response) {
    const items = response.items;
    state.records.total = count(response.total);
    state.records.page = Math.max(1, Math.floor(count(response.page) || state.records.page));
    const pages = Math.max(1, Math.ceil(state.records.total / state.records.size));
    text('#records-count', `共 ${number(state.records.total)} 条真实采集 / 标注记录`);
    $('#records-body').innerHTML = items.map(item => {
      const source = sourceDescription(item);
      const title = item.title || '未知曲目';
      const artist = item.artist || '未知歌手';
      const timezone = state.report?.timezone;
      return `<tr><td><div class="record-track">${cover(item.cover_id, 'record-cover')}<div class="record-info"><strong title="${e(title)}">${e(title)}</strong><span title="${e(artist)}">${e(artist)}</span></div></div></td><td title="${e(item.album || '')}">${e(item.album || '未知专辑')}</td><td class="record-date">${e(dateFormat(item.played_at, { year: 'numeric', month: '2-digit', day: '2-digit' }, timezone))}<small>${e(dateFormat(item.played_at, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }, timezone))}</small></td><td class="record-date">${e(shortDuration(item.duration_ms))}</td><td><span class="source-badge ${source.className}" title="${e(source.detail)}${item.source ? ` / ${e(item.source)}` : ''}">${source.label}</span></td></tr>`;
    }).join('');
    $('#records-empty').hidden = Boolean(items.length);
    if (!items.length) {
      $('#records-empty').innerHTML = emptyState('还没有符合条件的音迹', '试试其他周期或关键词；没有真实播放记录时，这份档案会保持空白。');
    }
    text('#records-page', `${state.records.page} / ${number(pages)}`);
    const first = state.records.total ? (state.records.page - 1) * state.records.size + 1 : 0;
    const last = Math.min(state.records.page * state.records.size, state.records.total);
    text('#pagination-description', state.records.total ? `第 ${number(first)} — ${number(last)} 条，共 ${number(state.records.total)} 条` : '每页 25 条');
    $('#records-prev').disabled = state.records.page <= 1;
    $('#records-next').disabled = state.records.page >= pages;
    $('#export-button').disabled = state.records.total === 0 || state.exportBusy;
    renderRecordFilterSummary();
  }
  async function loadRecords({ quiet = false } = {}) {
    if (!connected()) { renderEmptyRecords('login'); return; }
    if (state.records.period === 'report' && !state.report) {
      if (!state.reportFailed) await loadReport();
      if (!connected()) { renderEmptyRecords('login'); return; }
      if (!state.report) {
        renderEmptyRecords('error', '当前报告尚未读取，无法确定过滤周期。请重新读取报告，或选择“所有记录”。');
        state.recordsFailed = true;
        return;
      }
    }
    state.recordsController?.abort();
    const controller = new AbortController();
    state.recordsController = controller;
    const request = ++state.recordsRequest;
    state.recordsFailed = false;
    $('#records-error').hidden = true;
    $('#records-results').setAttribute('aria-busy', 'true');
    $('#export-button').disabled = true;
    $('#records-prev').disabled = true;
    $('#records-next').disabled = true;
    if (!quiet) {
      $('#records-body').innerHTML = '';
      $('#records-empty').hidden = false;
      $('#records-empty').innerHTML = '<div class="empty-state"><span class="skeleton" style="width:180px;height:13px" aria-hidden="true"></span><p>正在翻阅你的播放档案…</p></div>';
      text('#records-count', '正在读取播放记录');
    }
    try {
      const response = await api(`/api/records?${recordParams()}`, { signal: controller.signal });
      if (request !== state.recordsRequest || controller.signal.aborted) return;
      if (!response || !Array.isArray(response.items)) throw new ApiError('播放记录数据不完整，请重新读取。');
      renderRecords(response);
    } catch (error) {
      if (request !== state.recordsRequest || controller.signal.aborted) return;
      if (error.status === 401) { authExpired(); return; }
      state.recordsFailed = true;
      text('#records-error', error.message);
      $('#records-error').hidden = false;
      renderEmptyRecords('error', error.message);
    } finally {
      if (request === state.recordsRequest) $('#records-results').setAttribute('aria-busy', 'false');
    }
  }

  async function refreshAll() {
    if (state.refreshing) return;
    state.refreshing = true;
    hideError();
    try {
      const status = await loadStatus();
      if (status.error) showError(`飞牛连接：${typeof status.error === 'string' ? status.error : status.error.message || '连接出现问题，请查看连接设置。'}`);
      if (connected()) {
        await loadReport();
        if (state.view === 'records') await loadRecords();
      } else {
        state.reportController?.abort();
        state.recordsController?.abort();
        renderEmptyReport('login');
        renderEmptyRecords('login');
      }
    } catch (error) {
      state.statusFailed = true;
      renderStatus();
      showError(`连接状态：${error.message}`);
      if (!state.report) renderEmptyReport('error', error.message);
      if (state.view === 'records') renderEmptyRecords('error', error.message);
    } finally { state.refreshing = false; }
  }
  function openSettings() {
    state.lastFocus = document.activeElement;
    $('#connection-error').hidden = true;
    $('#password-input').value = '';
    renderStatus();
    if (state.status?.server) $('#server-input').value = state.status.server;
    if (state.status?.user?.name) $('#username-input').value = state.status.user.name;
    $('#connection-dialog').showModal();
    if (!connected()) $('#server-input').focus();
    else $('#close-settings').focus();
  }
  function closeSettings() {
    if (state.connectBusy || state.disconnectBusy) return;
    $('#password-input').value = '';
    $('#connection-dialog').close();
  }
  $('#connection-dialog').addEventListener('close', () => {
    $('#password-input').value = '';
    state.lastFocus?.focus();
  });
  $('#connection-dialog').addEventListener('cancel', event => {
    if (state.connectBusy || state.disconnectBusy) event.preventDefault();
  });
  $('#connection-dialog').addEventListener('click', event => {
    if (event.target === $('#connection-dialog')) {
      const rect = event.target.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeSettings();
    }
  });
  $('#open-settings').addEventListener('click', openSettings);
  $('#close-settings').addEventListener('click', closeSettings);
  $('#change-account').addEventListener('click', () => {
    $('#connection-form').hidden = !$('#connection-form').hidden;
    if (!$('#connection-form').hidden) $('#server-input').focus();
  });
  $('#restore-share').addEventListener('click', () => { window.location.href = '/?restore=1'; });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-connect]')) openSettings();
    if (event.target.closest('[data-retry]')) refreshAll();
    if (event.target.closest('[data-play]')) {
      const server = state.status?.server;
      if (server) window.open(server + '/music/', '_blank', 'noopener');
      else openSettings();
    }
  });
  $('#connection-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.connectBusy || !$('#connection-form').reportValidity()) return;
    const server = $('#server-input').value.trim();
    const username = $('#username-input').value.trim();
    let parsed;
    try { parsed = new URL(server); } catch { parsed = null; }
    if (!parsed || !['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || !username) {
      text('#connection-error', '请输入有效的 http:// 或 https:// 服务器地址及用户名。不要在网址中包含账号密码。');
      $('#connection-error').hidden = false;
      return;
    }
    state.connectBusy = true;
    $('#connection-error').hidden = true;
    $('#connect-submit').disabled = true;
    $('#close-settings').disabled = true;
    text('#connect-submit', '正在连接…');
    let success = false;
    try {
      const result = await api('/api/connect', { method: 'POST', body: JSON.stringify({ server, username, password: $('#password-input').value }) });
      if (result?.session) setStoredSession(result.session);
      $('#password-input').value = '';
      state.report = null;
      state.records.page = 1;
      await loadStatus();
      if (!connected()) throw new ApiError('登录成功但会话未建立，请确认浏览器允许本站 Cookie 后重试。');
      success = true;
      showToast('连接成功。正常使用飞牛音乐，播放会自动被记录。');
    } catch (error) {
      text('#connection-error', error.message);
      $('#connection-error').hidden = false;
    } finally {
      state.connectBusy = false;
      $('#connect-submit').disabled = false;
      $('#close-settings').disabled = false;
      $('#connect-submit').innerHTML = '连接飞牛音乐 <span aria-hidden="true">↗</span>';
      if (success) {
        closeSettings();
        hideError();
        await loadReport();
        if (state.view === 'records') await loadRecords();
      }
    }
  });
  $('#disconnect-button').addEventListener('click', async () => {
    if (state.disconnectBusy) return;
    state.disconnectBusy = true;
    $('#disconnect-button').disabled = true;
    $('#close-settings').disabled = true;
    $('#password-input').value = '';
    $('#connection-error').hidden = true;
    try {
      await api('/api/disconnect', { method: 'POST', body: '{}' });
      setStoredSession('');
      state.status = { connected: false, needs_login: true };
      state.reportController?.abort();
      state.recordsController?.abort();
      state.report = null;
      state.records.total = 0;
      state.records.page = 1;
      renderStatus();
      renderEmptyReport('login');
      renderEmptyRecords('login');
      hideError();
      showToast('已退出本设备会话；内网其他设备与播放记录不受影响。');
    } catch (error) {
      text('#connection-error', error.message);
      $('#connection-error').hidden = false;
      $('#connection-form').hidden = false;
    } finally {
      state.disconnectBusy = false;
      $('#disconnect-button').disabled = false;
      $('#close-settings').disabled = false;
    }
  });
  // 滚动进入视口时图表“长出来”：区块淡入上移，柱/条/射线按序生长；
  // 切换周期会重放图表动画。系统开启减少动态效果时全部瞬时呈现。
  const revealObserver = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add('revealed');
        revealObserver.unobserve(entry.target);
      }
    }
  }, { threshold: .18, rootMargin: '0px 0px -6% 0px' }) : null;
  function watchReveal(root = document) {
    const targets = $$('.reveal:not(.revealed)', root);
    if (!revealObserver) {
      targets.forEach(el => el.classList.add('revealed'));
      return;
    }
    targets.forEach(el => revealObserver.observe(el));
  }
  function replayCharts(root = document) {
    $$('.reveal.chart.revealed', root).forEach(el => el.classList.remove('revealed'));
  }

  // 分享按钮只复制真实报告摘要文字，不做任何对外发布。
  async function shareSummary() {
    const report = state.report;
    if (!report || !report.totals) return;
    const totals = report.totals;
    const summary = [
      `音迹 · ${PERIODS[state.period].title}（${rangeLabel(report)}）`,
      `播放 ${number(totals.plays)} 次 · 歌手 ${number(totals.artists)} 位 · 专辑 ${number(totals.albums)} 张 · 曲目 ${number(totals.tracks)} 首`,
      `曲目时长合计约 ${longDuration(totals.estimated_duration_ms)}（估算，非实际听完时长）`,
      '数据来自飞牛音乐真实播放开始事件'
    ].join('\n');
    try {
      await navigator.clipboard.writeText(summary);
      showToast('已复制本期摘要文字。');
    } catch {
      const area = document.createElement('textarea');
      area.value = summary;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand('copy');
      area.remove();
      showToast(copied ? '已复制本期摘要文字。' : '复制失败，请手动选择摘要文字。');
    }
  }
  $('#share-summary').addEventListener('click', shareSummary);

  // 年度 Playback 故事：按 Last.fm Playback 的真实版式复刻（章节配色、全出血封面、翻页导航），数据全部来自当年报告。
  const storyState = { open: false, observer: null, onScroll: null, onKey: null, returnFocus: null };
  const storyBg = coverId => coverId ? `<div class="story-backdrop" style="background-image:url('${coverSrc(coverId)}')"></div><div class="story-shade"></div>` : '<div class="story-shade plain"></div>';
  function buildStory(report) {
    const year = dateFormat(report.start, { year: 'numeric' }, report.timezone).replace('年', '');
    const totals = report.totals || {};
    const tops = report.tops || {};
    const discovery = report.discovery || {};
    const artist = (tops.artists || [])[0];
    const album = (tops.albums || [])[0];
    const track = (tops.tracks || [])[0];
    const moreAlbums = (tops.albums || []).slice(0, 4);
    const moreTracks = (tops.tracks || []).slice(0, 4);
    const totalHours = Math.max(0, Math.floor((totals.estimated_duration_ms || 0) / 3600000));
    const days = Math.floor(totalHours / 24);
    const minutes = Math.max(0, Math.round((totals.estimated_duration_ms || 0) / 60000));
    const tapes = Math.floor(minutes / 90);
    const moon = Math.floor(totalHours * 1235 / 384400);
    const discoveryShare = totals.artists ? Math.round((discovery.artists || 0) / totals.artists * 100) : 0;
    const listensPerTrack = totals.tracks ? (totals.plays / totals.tracks).toFixed(1) : '0.0';
    const peakHour = (report.hourly || []).reduce((best, plays, hour) => plays > (report.hourly[best] ?? -1) ? hour : best, 0);
    const hourlyMax = Math.max(1, ...(report.hourly || [1]));
    let streak = 0, bestStreak = 0, prevDay = null;
    for (const day of (report.calendar || [])) {
      if (day.plays > 0) {
        streak = prevDay && (new Date(day.date) - new Date(prevDay)) === 86400000 ? streak + 1 : 1;
        bestStreak = Math.max(bestStreak, streak);
        prevDay = day.date;
      } else { streak = 0; prevDay = null; }
    }
    const P = (cls, html) => `<section class="story-panel ${cls}"><div class="story-inner">${html}</div></section>`;
    const PF = (cls, coverId, html) => `<section class="story-panel story-feature ${cls}">${storyBg(coverId)}<div class="story-inner">${html}</div></section>`;
    const cards = items => `<div class="story-cards">${items.map((item, index) => `<figure>${cover(item.cover_id, 'story-card')}<figcaption>#${index + 1} ${e(item.name)}</figcaption><small>${e(item.artist || '')} · ${number(item.plays)} 次</small></figure>`).join('')}</div>`;
    const panels = [
      P('c-white', `<span class="story-brand">PLAYBACK</span><p class="story-year">${e(year)}</p><p class="story-caption">${e(state.status?.user?.name || '')} 的年度音乐故事</p><span class="story-hint">向下滚动</span>`),
      P('c-pink', `<p class="story-caption">${e(year)} 年，你花了</p><p class="story-big">${days ? `${number(days)} 天 ` : ''}${number(totalHours % 24)} 小时</p><p class="story-caption">听音乐</p>`),
      P('c-pink', `<p class="story-caption">这些时间足够录满</p><p class="story-big">${number(tapes)} 盘磁带</p>`),
      P('c-pink', `<p class="story-caption">也够沿着声波，飞往月球</p><p class="story-big">${number(moon)} 次</p>`),
      P('c-violet', `<p class="story-caption">这一年，你探索了</p><p class="story-big">${number(totals.artists ?? 0)} 位歌手</p><p class="story-caption">其中 ${number(discoveryShare)}% 是第一次听到</p><p class="story-caption">但只有一位，封王。</p>`),
      artist ? PF('story-feature c-violet', artist.cover_id, `<p class="story-caption">你今年的最爱歌手是</p><p class="story-name">${e(artist.name)}</p><p class="story-sub">你把 ${number(artist.plays)} 次播放给了 TA</p>`) : '',
      artist ? PF('story-feature c-violet', artist.cover_id, `<p class="story-caption">#1</p><p class="story-name">${e(artist.name)}</p><p class="story-sub">${number(artist.plays)} 次播放</p>`) : '',
      P('c-violet', `<p class="story-caption">不过，在你的年度里留下印记的，</p><p class="story-caption">不止 TA…</p>`),
      P('c-mint', `<p class="story-caption">你听了</p><p class="story-big">${number(totals.albums ?? 0)} 张专辑</p><p class="story-caption">看看是哪些专辑，让你一遍遍循环…</p>`),
      album ? PF('story-feature c-mint', album.cover_id, `<p class="story-caption">你今年的最爱专辑是</p><p class="story-name">${e(album.name)}</p><p class="story-sub">${e(album.artist || '')} · ${number(album.plays)} 次播放</p>`) : '',
      album ? PF('story-feature c-mint', album.cover_id, `<p class="story-caption">#1</p><p class="story-name">${e(album.name)}</p><p class="story-sub">${e(album.artist || '')} · ${number(album.plays)} 次播放</p>`) : '',
      moreAlbums.length ? P('c-mint', `<p class="story-caption">再看看你 ${e(year)} 年收藏的另外几张</p>${cards(moreAlbums)}`) : '',
      P('c-blue', `<p class="story-caption">你播放了</p><p class="story-big">${number(totals.tracks ?? 0)} 首不同曲目</p><p class="story-caption">平均每首约 ${e(listensPerTrack)} 次。其中一首，成为了你的年度之歌。</p>`),
      track ? PF('story-feature c-blue', track.cover_id, `<p class="story-caption">你今年的年度单曲是</p><p class="story-name">${e(track.name)}</p><p class="story-sub">${e(track.artist || '')} · ${number(track.plays)} 次播放</p>`) : '',
      track ? PF('story-feature c-blue', track.cover_id, `<p class="story-caption">#1</p><p class="story-name">${e(track.name)}</p><p class="story-sub">${e(track.artist || '')} · ${number(track.plays)} 次播放</p>`) : '',
      moreTracks.length ? P('c-blue', `<p class="story-caption">看看其他竞争者…</p>${cards(moreTracks)}`) : '',
      P('light c-dark', `<p class="story-caption">全年共</p><p class="story-big">${number(totals.plays ?? 0)} 次播放</p><p class="story-caption">活跃 ${number(totals.active_days ?? 0)} 天。而且，没有人像你这样听歌。</p>`),
      bestStreak > 1 ? P('light c-dark', `<p class="story-caption">你的最长连续纪录，是</p><p class="story-big">${number(bestStreak)} 天</p>`) : '',
      P('c-white', `<p class="story-caption">你最常在</p><p class="story-big">${String(peakHour).padStart(2, '0')}:00</p><p class="story-caption">打开音乐</p><div class="story-clock" aria-hidden="true">${(report.hourly || []).map((plays, hour) => `<i style="height:${Math.max(4, Math.round(plays / hourlyMax * 100))}%" title="${String(hour).padStart(2, '0')}:00 · ${number(plays)}"></i>`).join('')}</div>`),
      P('c-white', `<span class="story-brand">PLAYBACK</span><p class="story-caption">每一段播放，都在讲述故事。这是你的 ${e(year)} 年。</p><p class="story-count">${number(totals.plays ?? 0)} 次播放</p><p class="story-caption">2027 见。</p><div class="story-actions"><button type="button" class="button" id="story-share">分享摘要</button><button type="button" class="button button-outline" id="story-close">返回报告</button></div>`)
    ];
    $('#playback-scroll').innerHTML = panels.filter(Boolean).join('');
    $('#story-share').addEventListener('click', shareSummary);
    $('#story-close').addEventListener('click', closePlayback);
  }
  function openPlayback() {
    if (state.period !== 'year' || !state.report || storyState.open) return;
    buildStory(state.report);
    storyState.open = true;
    storyState.returnFocus = document.activeElement;
    $('#playback-story').hidden = false;
    document.body.classList.add('story-lock');
    const scroll = $('#playback-scroll');
    scroll.scrollTop = 0;
    scroll.focus({ preventScroll: true });
    if ('IntersectionObserver' in window) {
      storyState.observer = new IntersectionObserver(entries => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('in-view');
            storyState.observer.unobserve(entry.target);
          }
        }
      }, { root: scroll, threshold: .55 });
      $$('.story-panel', scroll).forEach(panel => storyState.observer.observe(panel));
    } else {
      $$('.story-panel', scroll).forEach(panel => panel.classList.add('in-view'));
    }
    storyState.onScroll = () => {
      const max = scroll.scrollHeight - scroll.clientHeight;
      $('#playback-progress-bar').style.width = `${max > 0 ? scroll.scrollTop / max * 100 : 100}%`;
      $('#playback-prev').disabled = scroll.scrollTop <= 2;
      $('#playback-next').disabled = scroll.scrollTop >= max - 2;
    };
    scroll.addEventListener('scroll', storyState.onScroll, { passive: true });
    storyState.onScroll();
    storyState.onKey = event => { if (event.key === 'Escape') closePlayback(); };
    document.addEventListener('keydown', storyState.onKey);
  }
  function closePlayback() {
    if (!storyState.open) return;
    storyState.open = false;
    $('#playback-story').hidden = true;
    document.body.classList.remove('story-lock');
    const scroll = $('#playback-scroll');
    scroll.removeEventListener('scroll', storyState.onScroll);
    document.removeEventListener('keydown', storyState.onKey);
    if (storyState.observer) { storyState.observer.disconnect(); storyState.observer = null; }
    if (storyState.returnFocus instanceof HTMLElement) storyState.returnFocus.focus();
  }
  $('#open-playback').addEventListener('click', openPlayback);
  $('#playback-exit').addEventListener('click', closePlayback);
  const storyStep = direction => { const scroll = $('#playback-scroll'); scroll.scrollBy({ top: direction * scroll.clientHeight, behavior: 'auto' }); };
  $('#playback-prev').addEventListener('click', () => storyStep(-1));
  $('#playback-next').addEventListener('click', () => storyStep(1));

  // 备份与恢复：完整导出已由下载按钮承担（经 fetch 携带会话头，兼容禁用 Cookie 的浏览器）；导入读取备份文件，合并去重或恢复替换。
  const backupState = { payload: null, restoreArmed: 0 };
  $('#download-backup').addEventListener('click', async () => {
    if (state.exportBusy) return;
    state.exportBusy = true;
    $('#download-backup').disabled = true;
    try {
      const response = await fetch('/api/export/full', { credentials: 'same-origin', cache: 'no-store', headers: sessionHeader() });
      if (!response.ok) throw new ApiError(`下载失败（HTTP ${response.status}）。`, response.status);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `音迹-完整备份-${localDate(new Date())}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      showToast('完整备份已下载，请保存到 NAS 之外的地方。');
    } catch (error) {
      if (error.status === 401) authExpired();
      else showError(`下载备份失败：${error.message}`);
    } finally {
      state.exportBusy = false;
      $('#download-backup').disabled = false;
    }
  });
  $('#import-choose').addEventListener('click', () => $('#backup-file').click());
  $('#backup-file').addEventListener('change', () => {
    const file = $('#backup-file').files[0];
    backupState.payload = null;
    $('#import-submit').disabled = true;
    $('#backup-summary').textContent = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const payload = JSON.parse(reader.result);
        if (!payload || payload.format !== 'yinji-backup' || !Array.isArray(payload.plays)) throw new Error('格式');
        backupState.payload = payload;
        const exported = payload.exported_at ? `，导出于 ${dateFormat(payload.exported_at, { year: 'numeric', month: '2-digit', day: '2-digit' }, state.report?.timezone)}` : '';
        $('#backup-summary').textContent = `已读取：${number(payload.plays.length)} 次播放 · ${number((payload.tracks || []).length)} 首曲目 · ${number((payload.artist_images || []).length)} 位歌手图片${exported}。`;
        $('#import-submit').disabled = false;
      } catch {
        $('#backup-summary').textContent = '无法读取：不是有效的音迹备份文件。';
      }
    };
    reader.readAsText(file);
  });
  $('#import-mode').addEventListener('change', () => { backupState.restoreArmed = 0; $('#import-submit').textContent = '导入'; });
  $('#import-submit').addEventListener('click', async () => {
    if (!backupState.payload || state.importBusy) return;
    const mode = $('#import-mode').value;
    if (mode === 'restore' && backupState.restoreArmed !== 2) {
      backupState.restoreArmed += 1;
      $('#import-submit').textContent = backupState.restoreArmed === 1 ? '再次点击确认恢复' : '确认：清空并恢复';
      setTimeout(() => { backupState.restoreArmed = 0; $('#import-submit').textContent = '导入'; }, 5000);
      return;
    }
    state.importBusy = true;
    $('#import-submit').disabled = true;
    try {
      const result = await api(`/api/import?mode=${mode}`, { method: 'POST', body: JSON.stringify(backupState.payload) });
      showToast(`导入完成：${number(result.plays || 0)} 次播放、${number(result.tracks || 0)} 首曲目、${number(result.artists || 0)} 位歌手${result.skipped ? `；跳过无效/重复 ${number(result.skipped)} 条` : ''}。`);
      backupState.restoreArmed = 0;
      $('#import-submit').textContent = '导入';
      $('#backup-file').value = '';
      $('#backup-summary').textContent = '';
      backupState.payload = null;
      $('#import-submit').disabled = true;
      await refreshAll();
    } catch (error) {
      if (error.status === 401) authExpired();
      else showError(`导入失败：${error.message}`);
    } finally {
      state.importBusy = false;
      $('#import-submit').disabled = !backupState.payload;
    }
  });


  async function setPeriod(period, offset) {
    if (!(period in PERIODS)) return;
    state.period = period;
    state.offset = Math.max(0, offset);
    state.report = null;
    state.records.page = 1;
    hideError();
    updatePeriodUI();
    await loadReport();
  }
  $$('[data-period]').forEach(button => button.addEventListener('click', () => {
    if (button.dataset.period !== state.period) setPeriod(button.dataset.period, 0);
  }));
  $('#older-report').addEventListener('click', () => setPeriod(state.period, state.offset + 1));
  $('#newer-report').addEventListener('click', () => { if (state.offset > 0) setPeriod(state.period, state.offset - 1); });
  $('#current-report').addEventListener('click', () => setPeriod(state.period, 0));
  $('#retry-button').addEventListener('click', refreshAll);

  // 日间/夜间切换：theme.js 在首帧前已按存储值设好 html 类，这里负责切换与记忆。
  const themeToggle = $('#theme-toggle');
  function applyTheme(theme) {
    const light = theme === 'light';
    document.documentElement.classList.toggle('theme-light', light);
    document.documentElement.classList.toggle('theme-dark', !light);
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('music-journal.theme', theme); } catch { /* storage may be blocked */ }
    themeToggle.setAttribute('aria-label', light ? '切换夜间模式' : '切换日间模式');
    if (state.report && state.period === 'year') renderCalendar(state.report.calendar || [], state.report);
  }
  themeToggle.addEventListener('click', () => {
    applyTheme(document.documentElement.classList.contains('theme-light') ? 'dark' : 'light');
  });
  applyTheme(document.documentElement.classList.contains('theme-light') ? 'light' : 'dark');

  async function setView(view, initial = false) {
    state.view = view === 'records' ? 'records' : view === 'overview' ? 'overview' : 'report';
    updatePeriodUI();
    $('#report-view').hidden = state.view === 'records';
    text('#report-heading', state.view === 'overview' ? '音乐概览' : '收听报告');
    $('#records-view').hidden = state.view !== 'records';
    $$('[data-view]').forEach(link => {
      const active = link.dataset.view === state.view;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    document.title = `${state.view === 'records' ? '播放记录' : PERIODS[state.period].title} · 音迹`;
    if (!initial) {
      $('#main').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'auto' });
    }
    if (state.view === 'records' && state.status) await loadRecords();
  }
  window.addEventListener('hashchange', () => setView(location.hash === '#records' ? 'records' : location.hash === '#overview' ? 'overview' : 'report'));
  $('#records-period').addEventListener('change', () => {
    const custom = $('#records-period').value === 'custom';
    $('#custom-date-fields').hidden = !custom;
    $('#records-start').required = custom;
    $('#records-end').required = custom;
    if (custom && !$('#records-start').value) {
      const range = state.report ? { start: epoch(state.report.start), end: epoch(state.report.end) } : localRange();
      $('#records-start').value = localDate(new Date(range.start));
      $('#records-end').value = localDate(new Date(range.end - 1));
    }
  });
  $('#records-filter').addEventListener('submit', async event => {
    event.preventDefault();
    if (!$('#records-filter').reportValidity()) return;
    const choice = $('#records-period').value;
    let start = null, end = null;
    $('#records-error').hidden = true;
    if (choice === 'custom') {
      const startDate = new Date(`${$('#records-start').value}T00:00:00`);
      const endDate = new Date(`${$('#records-end').value}T00:00:00`);
      endDate.setDate(endDate.getDate() + 1);
      start = startDate.getTime(); end = endDate.getTime();
      if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) {
        text('#records-error', '开始日期不能晚于结束日期，请重新选择。');
        $('#records-error').hidden = false;
        $('#records-start').focus();
        return;
      }
    }
    Object.assign(state.records, { period: choice, q: $('#records-search').value.trim(), start, end, page: 1 });
    renderRecordFilterSummary();
    await loadRecords();
  });
  $('#reset-filter').addEventListener('click', async () => {
    $('#records-filter').reset();
    $('#custom-date-fields').hidden = true;
    $('#records-start').required = false;
    $('#records-end').required = false;
    Object.assign(state.records, { period: 'report', q: '', start: null, end: null, page: 1 });
    await loadRecords();
  });
  $('#records-prev').addEventListener('click', async () => {
    if (state.records.page <= 1) return;
    state.records.page -= 1;
    await loadRecords();
    $('#records-heading').scrollIntoView({ block: 'start', behavior: 'auto' });
  });
  $('#records-next').addEventListener('click', async () => {
    if (state.records.page * state.records.size >= state.records.total) return;
    state.records.page += 1;
    await loadRecords();
    $('#records-heading').scrollIntoView({ block: 'start', behavior: 'auto' });
  });
  $('#export-button').addEventListener('click', async () => {
    if (!connected() || state.exportBusy || !state.records.total) return;
    state.exportBusy = true;
    $('#export-button').disabled = true;
    text('#export-button span', '正在导出');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const response = await fetch(`/api/export?${recordParams(false)}`, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: sessionHeader() });
      if (!response.ok) {
        let message = `导出失败（HTTP ${response.status}）。`;
        try { const data = await response.json(); message = data.error?.message || data.error || data.message || message; } catch { /* Keep HTTP detail. */ }
        throw new ApiError(String(message), response.status);
      }
      const type = response.headers.get('Content-Type') || '';
      if (type.includes('json') || type.includes('text/html')) throw new ApiError('服务器没有返回 CSV 文件，请检查会话或重试。');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `音迹-播放记录-${localDate(new Date())}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      showToast('CSV 已导出，包含当前筛选范围内的所有记录。');
    } catch (error) {
      if (error.status === 401) authExpired();
      else {
        text('#records-error', error.name === 'AbortError' ? '导出超时，请缩小日期范围后重试。' : error.message || '导出失败，请检查网络连接。');
        $('#records-error').hidden = false;
      }
    } finally {
      clearTimeout(timeout);
      state.exportBusy = false;
      text('#export-button span', '导出 CSV');
      $('#export-button').disabled = !connected() || !state.records.total || state.recordsFailed;
    }
  });

  // A lightweight status check reflects real plays from the proxied player, without a reload loop.
  async function pollStatus() {
    if (document.hidden || state.refreshing || state.connectBusy || state.disconnectBusy || $('#connection-dialog').open) return;
    try {
      const wasConnected = connected();
      const previousSignature = state.statusSignature;
      const status = await loadStatus();
      if (wasConnected && !connected()) {
        state.reportController?.abort(); state.recordsController?.abort();
        renderEmptyReport('login'); renderEmptyRecords('login');
      } else if (connected() && previousSignature !== state.statusSignature) {
        await loadReport({ quiet: true });
        if (state.view === 'records') await loadRecords({ quiet: true });
      }
      if (status.error) showError(`飞牛连接：${typeof status.error === 'string' ? status.error : status.error.message || '连接出现问题。'}`);
    } catch (error) {
      state.statusFailed = true;
      renderStatus();
      showError(`状态更新失败：${error.message}`);
    }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollStatus(); });
  window.setInterval(pollStatus, 45000);
  setView(location.hash === '#records' ? 'records' : location.hash === '#overview' ? 'overview' : 'report', true);
  renderLoadingCharts();
  refreshAll();
  // Deep links: #report/year 跳到年报告；#playback 与 #playback/<面板序号> 打开年度故事页。
  {
    const deepParts = location.hash.slice(1).split('/');
    if (deepParts[0] === 'report' && deepParts[1] === 'year') {
      const toYear = setInterval(() => {
        if (state.statusFailed || state.status?.needs_login) { clearInterval(toYear); return; }
        setPeriod('year', 0);
        clearInterval(toYear);
      }, 500);
      setTimeout(() => clearInterval(toYear), 6000);
    } else if (deepParts[0] === 'playback') {
      let deepOpen = true;
      const toStory = setInterval(() => {
        if (!deepOpen) { clearInterval(toStory); return; }
        if (state.statusFailed || state.status?.needs_login) { deepOpen = false; clearInterval(toStory); return; }
        if (state.period !== 'year' || !state.report) { setPeriod('year', 0); return; }
        openPlayback();
        if (storyState.open) {
          deepOpen = false;
          clearInterval(toStory);
          const index = parseInt(deepParts[1], 10);
          if (Number.isInteger(index) && index > 0) {
            const scroll = $('#playback-scroll');
            scroll.scrollTo({ top: index * scroll.clientHeight, behavior: 'auto' });
          }
        }
      }, 500);
      setTimeout(() => { deepOpen = false; clearInterval(toStory); }, 12000);
    }
  }
})();
