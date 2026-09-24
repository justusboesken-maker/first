'use strict';

const DATA_URL = 'data/signals.json';
const REFRESH_MS = 15 * 60 * 1000;
const NEW_SIGNAL_DAYS = 10;
const STALE_DAYS = 3;
const HISTORY_ROWS = 10;
const STORE = { notify: 'marktsignale:notify', seen: 'marktsignale:seen' };

const $ = (selector) => document.querySelector(selector);

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* privater Modus o. Ä. */ } },
};

/* Formatierung ----------------------------------------------------------- */

const numberFormat = (digits) => new Intl.NumberFormat('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmt = [0, 1, 2].map(numberFormat);
const pctFormat = new Intl.NumberFormat('de-DE', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const dayFormat = new Intl.DateTimeFormat('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
const stampFormat = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' });

const usd = (v) => `${fmt[Math.abs(v) >= 10000 ? 0 : 2].format(v)} $`;
const pct = (v) => pctFormat.format(v / 100);
const parseDay = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
const fmtDay = (iso) => dayFormat.format(parseDay(iso));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const signalName = (type) => (type === 'buy' ? 'Kaufsignal' : 'Verkaufssignal');

const ICON = {
  up: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M5 1.5 9 8.5H1z"/></svg>',
  down: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M5 8.5 1 1.5h8z"/></svg>',
  alert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 1 21h22L12 2Zm1 15h-2v-2h2v2Zm0-4h-2V9h2v4Z"/></svg>',
};

/* Zustand ---------------------------------------------------------------- */

let data = null;
let lastLoad = 0;
let showAllHistory = false;
const chartAssets = new Map();
const resizeObserver = 'ResizeObserver' in window
  ? new ResizeObserver((entries) => entries.forEach((e) => requestAnimationFrame(() => drawChart(e.target))))
  : null;

async function load() {
  lastLoad = Date.now();
  let next;
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(response.status === 404 ? 'missing' : `HTTP ${response.status}`);
    next = await response.json();
  } catch (error) {
    if (!data) renderEmpty(error.message === 'missing');
    return;
  }
  data = next;
  render();
  checkForNewSignals();
}

/* Darstellung ------------------------------------------------------------ */

function render() {
  renderUpdated();
  renderSummary();
  renderAssets();
  renderHistory();
}

function renderEmpty(missing) {
  $('#updated').textContent = 'Keine Daten';
  $('#assets').innerHTML = `
    <div class="empty">
      <h2>Noch keine Daten</h2>
      <p>${missing
        ? 'Die Signale werden von einem GitHub-Workflow berechnet. Starte in deinem Repository unter „Actions“ den Workflow „Signale aktualisieren“ einmal manuell – danach erscheinen hier die Signale.'
        : 'Die Daten konnten nicht geladen werden. Die Seite muss über einen Webserver aufgerufen werden (z. B. GitHub Pages), nicht direkt als Datei.'}</p>
    </div>`;
}

function renderUpdated() {
  const generated = new Date(data.generated_at);
  const stale = (Date.now() - generated.getTime()) / 86400000 > STALE_DAYS;
  $('#updated').innerHTML = `Stand: ${esc(stampFormat.format(generated))} Uhr${stale ? ' · <span class="stale">Daten veraltet</span>' : ''}`;
}

function renderSummary() {
  const invested = data.assets.filter((a) => a.status === 'invested').length;
  $('#summary').innerHTML = [
    `<span class="chip"><strong>${invested} von ${data.assets.length}</strong> investiert</span>`,
    ...data.assets.map((a) => `
      <a class="chip link" href="#asset-${esc(a.id)}">
        <span class="dot ${a.status}"></span><strong>${esc(a.name)}</strong>
        ${a.status === 'invested' ? 'investiert' : 'nicht investiert'}
      </a>`),
  ].join('');
}

function isRecent(iso) {
  return (new Date(data.generated_at) - parseDay(iso)) / 86400000 <= NEW_SIGNAL_DAYS;
}

function callout(kind, html) {
  const icon = kind === 'warn' ? ICON.alert : kind === 'buy' ? ICON.up : ICON.down;
  return `<div class="callout ${kind}">${icon}<div>${html}</div></div>`;
}

function delta(value, suffix) {
  const dir = value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
  const arrow = dir === 'up' ? '↑' : dir === 'down' ? '↓' : '→';
  return `<span class="delta ${dir}">${arrow} ${pct(value)}${suffix}</span>`;
}

function fact(label, value, hint) {
  return `<div><dt>${label}</dt><dd>${value}${hint ? `<span class="hint">${hint}</span>` : ''}</dd></div>`;
}

function triggerFact(asset) {
  const { trigger, rule } = asset;
  const side = trigger.type === 'sell' ? 'unter' : 'über';
  const label = signalName(trigger.type);
  if (trigger.needed > 1) {
    const dots = Array.from({ length: trigger.needed }, (_, i) => `<i class="${i < trigger.streak ? `on ${trigger.type}` : ''}"></i>`).join('');
    return fact(label, `<span class="dots" aria-hidden="true">${dots}</span>${trigger.streak} von ${trigger.needed} Wochen`,
      `Schlüsse in Folge ${side} dem MA`);
  }
  return fact(label, `${side} ${usd(trigger.level)}`,
    rule.band_pct ? `Wochenschluss ${fmt[0].format(rule.band_pct)} % ${side} MA` : 'Wochenschluss');
}

function cardHtml(asset) {
  const invested = asset.status === 'invested';
  const week = asset.last_week;
  const signal = asset.last_signal;
  const live = asset.live;

  const callouts = [];
  if (asset.error) {
    callouts.push(callout('warn', `<strong>Aktualisierung fehlgeschlagen.</strong> Angezeigt wird der Stand vom Wochenschluss ${fmtDay(week.date)}.`));
  }
  if (signal && isRecent(signal.date)) {
    callouts.push(callout(signal.type, `<strong>Neues ${signalName(signal.type)}</strong> zum Wochenschluss am ${fmtDay(signal.date)} bei ${usd(signal.close)}.`));
  }
  if (live && live.would_signal) {
    callouts.push(callout('warn', `Schließt die laufende Woche auf dem aktuellen Niveau, entsteht ein <strong>${signalName(live.would_signal)}</strong>.`));
  }

  const facts = [fact('50-Wochen-MA', usd(week.ma))];
  if (signal) {
    facts.push(fact(`Seit ${signal.type === 'buy' ? 'Kauf' : 'Verkauf'}`, delta(asset.change_since_signal_pct, ''),
      `Signal am ${fmtDay(signal.date)} bei ${usd(signal.close)}`));
  } else {
    facts.push(fact('Letztes Signal', '–', 'keins im Datenzeitraum'));
  }
  facts.push(triggerFact(asset));
  if (live) {
    facts.push(fact('Laufende Woche', `${usd(live.close)} ${delta(live.distance_pct, '')}`,
      `vorläufig, Stand ${fmtDay(live.date)}`));
  }

  return `
    <article class="card" id="asset-${esc(asset.id)}">
      <div class="info">
        ${callouts.length ? `<div class="callout-stack">${callouts.join('')}</div>` : ''}
        <h2>${esc(asset.name)}</h2>
        <p class="instrument">${esc(asset.symbol)} · ${esc(asset.instrument)}</p>
        <div class="status-row">
          <span class="badge ${asset.status}">${invested ? 'Investiert' : 'Nicht investiert'}</span>
          ${signal ? `<span class="since">seit ${fmtDay(signal.date)}</span>` : ''}
        </div>
        <div class="figure">
          <div class="figure-label">Wochenschluss ${fmtDay(week.date)}</div>
          <div class="figure-row">
            <span class="figure-value">${usd(week.close)}</span>
            ${delta(week.distance_pct, ' zum MA')}
          </div>
        </div>
        <dl class="facts">${facts.join('')}</dl>
        <p class="rule"><strong>Kauf:</strong> ${esc(asset.rule.buy)}<br><strong>Verkauf:</strong> ${esc(asset.rule.sell)}</p>
      </div>
      <div class="chart-col">
        <div class="chart-head">
          <span class="chart-title">Wochenschlüsse · letzte 3 Jahre</span>
          <div class="legend">
            <span><i class="key-line price"></i>Kurs</span>
            <span><i class="key-line ma"></i>50W-MA</span>
            <span><i class="key-box"></i>investiert</span>
            <span><svg class="key-mark" viewBox="0 0 10 10" aria-hidden="true"><path d="M5 1 9.5 9H.5z" fill="var(--good)"/></svg>Kauf</span>
            <span><svg class="key-mark" viewBox="0 0 10 10" aria-hidden="true"><path d="M5 9 .5 1h9z" fill="var(--bad)"/></svg>Verkauf</span>
          </div>
        </div>
        <div class="chart" data-asset="${esc(asset.id)}" role="img"
             aria-label="${esc(`${asset.name}: Wochenschlusskurse und 50-Wochen-MA der letzten drei Jahre, Phasen mit Investition hervorgehoben`)}"></div>
      </div>
    </article>`;
}

function renderAssets() {
  resizeObserver?.disconnect();
  chartAssets.clear();
  $('#assets').innerHTML = data.assets.map(cardHtml).join('');
  document.querySelectorAll('.chart').forEach((el) => {
    chartAssets.set(el, data.assets.find((a) => a.id === el.dataset.asset));
    drawChart(el);
    resizeObserver?.observe(el);
  });
}

function renderHistory() {
  const all = data.assets
    .flatMap((a) => a.signals.map((s) => ({ ...s, asset: a.name })))
    .sort((p, q) => q.date.localeCompare(p.date));
  $('#history').hidden = all.length === 0;
  const rows = showAllHistory ? all : all.slice(0, HISTORY_ROWS);
  $('#history-body').innerHTML = rows.map((s) => `
    <tr>
      <td>${fmtDay(s.date)}</td>
      <td>${esc(s.asset)}</td>
      <td><span class="tag ${s.type}">${s.type === 'buy' ? `${ICON.up}Kauf` : `${ICON.down}Verkauf`}</span></td>
      <td class="num">${usd(s.close)}</td>
      <td class="num">${pct(s.distance_pct)}</td>
    </tr>`).join('');
  const toggle = $('#history-toggle');
  toggle.hidden = all.length <= HISTORY_ROWS;
  toggle.textContent = showAllHistory ? 'Weniger anzeigen' : `Alle ${all.length} anzeigen`;
}

/* Chart ------------------------------------------------------------------ */

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  // kleinster runder Abstand, der nicht mehr als count + 1 Linien ergibt
  const step = [1, 2, 5, 10, 20].map((f) => f * magnitude).find((s) => (hi - lo) / s <= count + 1);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(Number(v.toFixed(8)));
  return { ticks, digits: Math.min(2, Math.max(0, -Math.floor(Math.log10(step)))) };
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function drawChart(el) {
  const asset = chartAssets.get(el);
  if (!asset) return;
  const rows = asset.chart.filter((r) => r.ma !== null);
  const width = el.clientWidth;
  const height = el.clientHeight;
  if (rows.length < 2 || width < 50) return;

  const pad = { top: 10, right: 58, bottom: 26, left: 2 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const n = rows.length;
  const x = (i) => pad.left + (i / (n - 1)) * plotW;

  let lo = Infinity;
  let hi = -Infinity;
  rows.forEach((r) => { lo = Math.min(lo, r.close, r.ma); hi = Math.max(hi, r.close, r.ma); });
  const span = hi - lo || hi || 1;
  lo -= span * 0.06;
  hi += span * 0.06;
  const y = (v) => pad.top + (1 - (v - lo) / (hi - lo)) * plotH;
  const { ticks, digits } = niceTicks(lo, hi, Math.max(2, Math.round(plotH / 55)));

  const parts = [];
  // investierte Phasen
  for (let i = 0; i < n; i++) {
    if (!rows[i].invested) continue;
    let j = i;
    while (j + 1 < n && rows[j + 1].invested) j++;
    const x0 = x(i);
    const x1 = x(Math.min(j + 1, n - 1));
    parts.push(`<rect class="band" x="${x0.toFixed(1)}" y="${pad.top}" width="${Math.max(x1 - x0, 2).toFixed(1)}" height="${plotH}"/>`);
    i = j;
  }
  // Raster & Achsen
  parts.push('<g class="grid">');
  ticks.forEach((t) => parts.push(`<line x1="${pad.left}" x2="${pad.left + plotW}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>`));
  parts.push('</g>');
  ticks.forEach((t) => parts.push(`<text class="tick" x="${pad.left + plotW + 8}" y="${y(t).toFixed(1)}" dy="0.35em">${fmt[digits].format(t)}</text>`));
  for (let i = 1; i < n; i++) {
    if (rows[i].date.slice(0, 4) === rows[i - 1].date.slice(0, 4)) continue;
    const xi = x(i);
    parts.push(`<line class="cursor" x1="${xi.toFixed(1)}" x2="${xi.toFixed(1)}" y1="${pad.top + plotH}" y2="${pad.top + plotH + 5}"/>`);
    if (xi < pad.left + plotW - 24) {
      parts.push(`<text class="tick" x="${(xi + 4).toFixed(1)}" y="${pad.top + plotH + 17}">${rows[i].date.slice(0, 4)}</text>`);
    }
  }
  // Linien
  const line = (key) => rows.map((r, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join('');
  parts.push(`<path class="line ma" d="${line('ma')}"/>`);
  parts.push(`<path class="line price" d="${line('close')}"/>`);
  // Signale
  const index = new Map(rows.map((r, i) => [r.date, i]));
  const signalsByDate = new Map();
  asset.signals.forEach((s) => {
    const i = index.get(s.date);
    if (i === undefined) return;
    signalsByDate.set(s.date, s);
    const cx = x(i);
    const cy = y(s.close);
    const d = s.type === 'buy'
      ? `M${cx},${cy - 7}L${cx + 6.5},${cy + 5}L${cx - 6.5},${cy + 5}Z`
      : `M${cx},${cy + 7}L${cx + 6.5},${cy - 5}L${cx - 6.5},${cy - 5}Z`;
    parts.push(`<path class="marker ${s.type}" d="${d}"/>`);
  });
  // Hover-Ebene
  parts.push(`<g class="hover" visibility="hidden">
    <line class="cursor" y1="${pad.top}" y2="${pad.top + plotH}"/>
    <circle class="cursor-dot" r="4" fill="var(--ma)"/>
    <circle class="cursor-dot" r="4.5" fill="var(--price)"/>
  </g>`);

  el.innerHTML = `<svg xmlns="${SVG_NS}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${parts.join('')}</svg>`;

  const svg = el.firstElementChild;
  const hover = svg.querySelector('.hover');
  const [cursor, maDot, priceDot] = hover.children;
  const tooltip = $('#tooltip');

  const show = (event) => {
    const box = svg.getBoundingClientRect();
    const i = Math.min(n - 1, Math.max(0, Math.round(((event.clientX - box.left - pad.left) / plotW) * (n - 1))));
    const r = rows[i];
    const xi = x(i).toFixed(1);
    cursor.setAttribute('x1', xi);
    cursor.setAttribute('x2', xi);
    priceDot.setAttribute('cx', xi);
    priceDot.setAttribute('cy', y(r.close).toFixed(1));
    maDot.setAttribute('cx', xi);
    maDot.setAttribute('cy', y(r.ma).toFixed(1));
    hover.setAttribute('visibility', 'visible');

    const signal = signalsByDate.get(r.date);
    tooltip.innerHTML = `
      <div class="tt-date">Wochenschluss ${fmtDay(r.date)}</div>
      <div class="tt-row"><span><i class="tt-key" style="background:var(--price)"></i>Kurs</span><b>${usd(r.close)}</b></div>
      <div class="tt-row"><span><i class="tt-key" style="background:var(--ma)"></i>50W-MA</span><b>${usd(r.ma)}</b></div>
      <div class="tt-row"><span>Abstand</span><b>${pct((r.close / r.ma - 1) * 100)}</b></div>
      <div class="tt-row"><span>Status</span><b>${r.invested ? 'investiert' : 'nicht investiert'}</b></div>
      ${signal ? `<div class="tt-signal ${signal.type}">${signal.type === 'buy' ? '▲' : '▼'} ${signalName(signal.type)}</div>` : ''}`;
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth;
    const tipH = tooltip.offsetHeight;
    const pointX = box.left + x(i);
    let left = pointX + 16;
    if (left + tipW > window.innerWidth - 8) left = pointX - tipW - 16;
    const top = Math.min(window.innerHeight - tipH - 8, Math.max(8, event.clientY - tipH / 2));
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${top}px`;
  };
  const hide = () => {
    hover.setAttribute('visibility', 'hidden');
    tooltip.hidden = true;
  };
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('pointercancel', hide);
}

/* Browser-Benachrichtigungen --------------------------------------------- */

const notifySupported = () => 'Notification' in window;
const notifyEnabled = () => notifySupported() && Notification.permission === 'granted' && store.get(STORE.notify) === 'on';

function renderNotifyButton() {
  const button = $('#notify-btn');
  if (!notifySupported()) return;
  const denied = Notification.permission === 'denied';
  const enabled = notifyEnabled();
  button.hidden = false;
  button.disabled = denied;
  button.setAttribute('aria-pressed', String(enabled));
  $('#notify-label').textContent = denied
    ? 'Benachrichtigungen blockiert'
    : enabled ? 'Benachrichtigungen an' : 'Benachrichtigungen aktivieren';
  button.title = denied
    ? 'Benachrichtigungen in den Browser-Einstellungen für diese Seite erlauben.'
    : 'Meldet neue Signale, solange diese Seite in einem Tab geöffnet ist.';
}

function showNotification(title, body) {
  try {
    new Notification(title, { body, icon: 'assets/favicon.svg', tag: title });
  } catch {
    // Manche mobilen Browser erlauben Benachrichtigungen nur über Service Worker.
  }
}

async function toggleNotifications() {
  if (notifyEnabled()) {
    store.set(STORE.notify, 'off');
  } else {
    const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    if (permission === 'granted') {
      store.set(STORE.notify, 'on');
      showNotification('Benachrichtigungen aktiv', 'Neue Kauf- und Verkaufssignale werden gemeldet, solange die Seite geöffnet ist.');
    }
  }
  renderNotifyButton();
}

function checkForNewSignals() {
  let seen = null;
  try { seen = JSON.parse(store.get(STORE.seen) || 'null'); } catch { seen = null; }
  if (Array.isArray(seen) && notifyEnabled()) {
    const known = new Set(seen);
    data.assets.forEach((asset) => {
      const s = asset.last_signal;
      if (!s || known.has(s.id)) return;
      showNotification(`${signalName(s.type)}: ${asset.name}`,
        `Wochenschluss ${fmtDay(s.date)}: ${usd(s.close)} (${pct(s.distance_pct)} zum 50-Wochen-MA)`);
    });
  }
  store.set(STORE.seen, JSON.stringify(data.assets.map((a) => a.last_signal?.id).filter(Boolean)));
}

/* Start ------------------------------------------------------------------ */

$('#notify-btn').addEventListener('click', toggleNotifications);
$('#history-toggle').addEventListener('click', () => { showAllHistory = !showAllHistory; renderHistory(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && Date.now() - lastLoad > 5 * 60 * 1000) load();
});
setInterval(load, REFRESH_MS);
renderNotifyButton();
load();
