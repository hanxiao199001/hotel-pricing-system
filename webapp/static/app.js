/* ==========================================================================
   小海 · 酒店智能决策中台 —— 前端
   真实页：概览 / 价格雷达 / 调价建议 / 系统状态
   示例页：差评预警 / 口碑分析 / 经营报表 / 客源结构 / 办公协同 / 集团驾驶舱
   ========================================================================== */
'use strict';

const PAGES = [
  { key: 'overview',    group: '经营', title: '概览',           sub: '今日该不该调价，先看这里', ico: '◧', live: true },
  { key: 'radar',       group: '经营', title: '价格雷达',       sub: '周边竞品实时价格与分布',   ico: '◎', live: true },
  { key: 'suggestions', group: '经营', title: '调价建议',       sub: '建议模式：人拍板才算数',   ico: '✎', live: true },
  { key: 'reviews',     group: '口碑', title: '差评预警',       sub: '真实点评归类 · 只读快照', ico: '!', live: false, snapshot: true },
  { key: 'replies',     group: '口碑', title: '差评回复',       sub: '生成草稿 · 人工确认后发布', ico: '✉', live: false, snapshot: true },
  { key: 'reputation',  group: '口碑', title: '口碑分析',       sub: '四维拆解 · 房型与定价',   ico: '❖', live: false, snapshot: true },
  { key: 'reports',     group: '经营', title: '经营报表',       sub: '周报月报 · 数字对得上账',  ico: '▤', live: false },
  { key: 'guests',      group: '经营', title: '客源结构',       sub: '客源地 / 渠道 / 复购',    ico: '◈', live: false },
  { key: 'tasks',       group: '协同', title: '办公协同',       sub: '工单排班与跨部门提醒',     ico: '☑', live: false },
  { key: 'group',       group: '集团', title: '集团驾驶舱',     sub: '只看共享池与匿名计数',     ico: '⛭', live: false },
  { key: 'system',      group: '系统', title: '系统状态',       sub: '数据源、文件、采集任务',   ico: '⚙', live: true },
];

const state = {
  page: 'overview',
  status: null,
  jobId: null,
  jobTimer: null,
  radarSort: { key: 'price', dir: 'asc' },
  radarDate: null,
  sim: null,
};

/* ---------------- 工具 ---------------- */
const $ = (s, root) => (root || document).querySelector(s);

function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
const money = (n) => (n == null || n === '' ? '—' : '¥' + Math.round(Number(n)).toLocaleString('zh-CN'));
const num = (n, d) => (n == null ? '—' : Number(n).toFixed(d == null ? 1 : d));
const pct = (n) => (n == null ? '—' : Number(n).toFixed(1) + '%');

async function apiGet(path) {
  const r = await fetch(path, { headers: { 'Accept': 'application/json' } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function toast(msg, kind) {
  const box = $('#toasts');
  const d = document.createElement('div');
  d.className = 'toast ' + (kind || '');
  d.textContent = msg;
  box.appendChild(d);
  setTimeout(() => { d.style.opacity = '0'; d.style.transition = 'opacity .3s'; }, 2600);
  setTimeout(() => d.remove(), 3000);
}

/* ---------------- 通用组件 ---------------- */
function kpiGrid(items) {
  return '<div class="kpi-grid">' + items.map((k) => `
    <div class="kpi ${k.tone || ''}">
      <div class="k-label">${esc(k.label)}${k.source ? srcDot(k.source) : ''}</div>
      <div class="k-num">${esc(k.value)}</div>
      ${k.sub ? `<div class="k-sub">${esc(k.sub)}</div>` : ''}
    </div>`).join('') + '</div>';
}

function srcDot(source) {
  if (source === 'live') return '<span class="status-dot ok" title="实时数据"></span>';
  if (source === 'snapshot') return '<span class="status-dot snap" title="历史快照"></span>';
  if (source === 'static') return '<span class="status-dot warn" title="示例数据"></span>';
  return '<span class="status-dot warn" title="含示例数据"></span>';
}

function card(title, bodyHtml, meta) {
  return `<div class="card">
    ${title ? `<div class="card-head"><b class="card-title">${esc(title)}</b>${meta ? `<span class="card-meta">${esc(meta)}</span>` : ''}</div>` : ''}
    ${bodyHtml}
  </div>`;
}

function linkRows(items) {
  return items.map((it) => `<div class="link-row">
    <span class="lk">${esc(it.label)}</span>
    <span class="rv ${it.tone || ''}">${esc(it.value)}</span>
  </div>`).join('');
}

function barsBlock(block) {
  const suffix = block.suffix || '';
  // max 缺省 100：老数据用百分比当宽度；差评条数这类计数需要 max 归一化
  const max = Number(block.max) || 100;
  return '<div class="bars">' + block.items.map((it) => `
    <div class="bar-row">
      <div class="bar-top"><span>${esc(it.label)}</span><b>${esc(it.value)}${suffix}</b></div>
      <div class="bar-track"><div class="bar-fill ${it.tone || ''}" style="width:${Math.max(2, Math.min(100, (Number(it.value) / max) * 100))}%"></div></div>
      ${it.detail ? `<div class="bar-detail">${esc(it.detail)}</div>` : ''}
    </div>`).join('') + '</div>';
}

function tableBlock(block) {
  const headCell = (c) => {
    const cls = [c.align === 'right' ? 'r' : '', c.nowrap ? 'nw' : ''].filter(Boolean).join(' ');
    const style = c.width ? ` style="width:${esc(c.width)}"` : '';
    return `<th class="${cls}"${style}>${esc(c.label)}</th>`;
  };
  const heads = block.columns.map(headCell).join('');
  const rows = block.rows.map((row) => {
    const tds = block.columns.map((c) => {
      const v = row[c.key];
      const tone = row[c.key + 'Tone'] || '';
      const cls = [c.align === 'right' ? 'r' : '', c.nowrap ? 'nw' : '', tone].filter(Boolean).join(' ');
      return `<td class="${cls}">${esc(v == null ? '—' : v)}</td>`;
    }).join('');
    return `<tr class="${row.rowTone || ''}">${tds}</tr>`;
  }).join('');
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr>${heads}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function quoteBlock(block) {
  return `<div class="quote">${esc(block.text)}${block.footnote ? `<div class="quote-foot">${esc(block.footnote)}</div>` : ''}</div>`;
}

function renderBlocks(blocks) {
  return (blocks || []).map((b) => {
    switch (b.type) {
      case 'kpi':
        return kpiGrid(b.items);
      case 'list':
        return card(b.title, linkRows(b.items));
      case 'table':
        return card(b.title, tableBlock(b), b.meta);
      case 'bars':
        return card(b.title, barsBlock(b), b.meta);
      case 'quote':
        return card(b.title, quoteBlock(b));
      case 'banner':
        return `<div class="banner warm">${esc(b.text)}</div>`;
      case 'note':
        return `<div class="note">${esc(b.text)}</div>`;
      default:
        return '';
    }
  }).join('');
}

/* ---------------- 导航 ---------------- */
function renderNav() {
  const groups = [];
  PAGES.forEach((p) => {
    let g = groups.find((x) => x.name === p.group);
    if (!g) { g = { name: p.group, items: [] }; groups.push(g); }
    g.items.push(p);
  });
  $('#nav').innerHTML = groups.map((g) => `
    <div class="nav-sec">${esc(g.name)}</div>
    ${g.items.map((p) => `
      <button class="nav-item ${p.key === state.page ? 'on' : ''}" data-page="${p.key}">
        <span class="ico">${p.ico}</span>
        <span>${esc(p.title)}</span>
        <span class="${p.live ? 'dot-live' : (p.snapshot ? 'dot-snap' : 'dot-static')}" title="${p.live ? '实时数据' : (p.snapshot ? '历史快照' : '示例数据')}"></span>
      </button>`).join('')}
  `).join('');

  $('#nav').querySelectorAll('[data-page]').forEach((btn) => {
    btn.addEventListener('click', () => { location.hash = '#/' + btn.dataset.page; });
  });
}

/* ---------------- 路由 ---------------- */
function route() {
  const key = (location.hash || '').replace(/^#\/?/, '') || 'overview';
  state.page = PAGES.some((p) => p.key === key) ? key : 'overview';
  renderNav();
  renderPage();
}

async function renderPage() {
  const meta = PAGES.find((p) => p.key === state.page);
  $('#pageTitle').textContent = meta.title;
  $('#pageSub').textContent = meta.sub;
  const badge = meta.live ? { cls: 'live', text: '实时数据' }
    : (meta.snapshot ? { cls: 'snapshot', text: '历史快照' } : { cls: 'static', text: '示例数据' });
  $('#srcBadge').className = 'badge ' + badge.cls;
  $('#srcBadge').innerHTML = `<i></i>${badge.text}`;

  const box = $('#content');
  box.innerHTML = '<div class="loading">正在加载…</div>';
  try {
    if (state.page === 'overview') await renderOverview(box);
    else if (state.page === 'radar') await renderRadar(box);
    else if (state.page === 'suggestions') await renderSuggestions(box);
    else if (state.page === 'replies') await renderReplies(box);
    else if (state.page === 'system') await renderSystem(box);
    else await renderStatic(box, state.page);
  } catch (err) {
    box.innerHTML = `<div class="card"><div class="empty"><b>加载失败</b>${esc(err.message)}</div></div>`;
  }
}

/* ==========================================================================
   页面：概览
   ========================================================================== */
async function renderOverview(box) {
  const d = await apiGet('/api/overview');
  const r = d.radar;
  const sug = d.suggestion;

  const srcBar = `<div class="srcbar ${r.empty ? '' : 'live'}">
    <b>${r.empty ? '暂无采集数据' : '实时数据'}</b>
    <span>${r.empty
      ? '库里还没有竞品记录。点右上角「立即采集」跑一次，或先在命令行执行 python main.py。'
      : `竞品价格来自 ${esc(r.date)} 的携程采集（半径 ${esc(r.distanceKm)} km，${r.stats.count} 家）；入住率为人工填写。`}</span>
  </div>`;

  const priceHtml = sug ? decisionCard(sug, { compact: true }) : `
    <div class="empty"><b>还没有可用的调价建议</b>先跑一次采集，系统会基于同档竞品中位价给出建议。</div>`;

  // 房型口碑 → 定价：只给判断，不改建议价（算价仍在算法层完成）
  const rs = d.roomSignal || {};
  const rsTone = rs.baseSignal
    ? (rs.baseSignal.level === 'weak' ? 'warm'
      : (rs.baseSignal.level === 'strong' ? 'ok' : 'muted'))
    : 'muted';
  const roomSignHtml = (rs.available && rs.baseSignal) ? `
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
      <div class="link-row" data-go="reputation" style="cursor:pointer">
        <span class="lk">起价房型口碑 · ${esc(rs.baseTier)}</span>
        <span class="rv ${rsTone}">${esc(rs.baseSignal.label)} ↗</span>
      </div>
      <div class="note" style="margin-top:8px">${esc(rs.baseSignal.hint)}</div>
    </div>` : '';

  const ladder = r.empty ? '' : ladderHtml(r.stats, r.myPrice || d.hotel.currentPrice);

  const pendingHtml = (d.pendingSuggestions && d.pendingSuggestions.length)
    ? d.pendingSuggestions.map((s) => `
        <div class="link-row">
          <span class="lk">调价建议 · ${esc(s.date)} · ${money(s.current_price)} → ${money(s.suggested_price)}</span>
          <span class="rv warm">待拍板</span>
        </div>`).join('')
    : '<div class="note">没有待拍板的调价建议。</div>';

  const rv = d.reviews || {};
  const reviewNote = rv.available
    ? (rv.unreplied
        ? `另有 ${rv.unreplied} 条差评待回复（${esc(rv.snapshotDate)} 快照）。`
        : `${rv.bad} 条差评均已回复（${esc(rv.snapshotDate)} 快照）。`)
    : '差评模块为示例数据。';

  box.innerHTML = srcBar
    + kpiGrid(d.kpis)
    + `<div class="grid side-main">
        <div class="card">
          <div class="card-head">
            <b class="card-title">调价建议 · 起价口径</b>
            <span class="card-meta">${r.empty ? '无数据' : '算法：同档中位价 × 入住率系数'}</span>
          </div>
          ${priceHtml}
          ${roomSignHtml}
        </div>
        <div style="display:flex;flex-direction:column;gap:16px">
          ${card('等你拍板', pendingHtml + `<div class="note" style="margin-top:10px">${reviewNote}</div>`)}
          ${card('竞品两极', r.empty ? '<div class="note">无数据</div>' : `
            <div class="note" style="margin-bottom:6px">最贵的 3 家</div>
            ${r.topExpensive.map((c) => `<div class="link-row"><span class="lk">${esc(c.name)}</span><span class="rv warm">${money(c.price)} · ${num(c.distance_km, 2)}km</span></div>`).join('')}
            <div class="note" style="margin:12px 0 6px">最便宜的 3 家</div>
            ${r.topCheap.map((c) => `<div class="link-row"><span class="lk">${esc(c.name)}</span><span class="rv ok">${money(c.price)} · ${num(c.distance_km, 2)}km</span></div>`).join('')}
          `)}
        </div>
      </div>`
    + (ladder ? card('我方位置（竞品价格轴）', ladder, r.stats ? `${r.stats.count} 家 · ${money(r.stats.min)} ~ ${money(r.stats.max)}` : '') : '')
    + card('模块入口', `<div class="tiles">
        ${[['reviews', '差评预警',
             rv.available ? `${rv.bad} 条差评` : '—',
             rv.available ? `只看不回 · ${esc(rv.snapshotDate)} 快照` : '示例数据'],
           ['replies', '差评回复',
             rv.available ? `${rv.bad} 条可起草` : '—',
             '模板/模型 · 人工确认'],
           ['reputation', '口碑分析',
             rv.available ? `综合 ${num(rv.avg, 2)}` : '—',
             rv.available ? '四维拆解 · 房型定价' : '示例数据'],
           ['reports', '经营报表', 'RevPAR ¥216', '示例 · 需 PMS 接入'],
           ['guests', '客源结构', 'OTA 客源 48%', '示例 · 需订单数据'],
           ['tasks', '办公协同', '12 张工单', '示例 · 需工单系统'],
           ['group', '集团驾驶舱', '12 家门店', '示例 · 需多店部署']]
          .map(([k, t, v, note]) => `<div class="tile" data-go="${k}">
            <b>${t}</b><span>${v}</span><em>${note}</em>
          </div>`).join('')}
      </div>
      <div class="note" style="margin-top:10px">${rv.available
        ? `差评、口碑、回复三页已接真实点评快照（截至 ${esc(rv.snapshotDate)}，${Number(rv.kept).toLocaleString('zh-CN')} 条）；其余 4 个模块仍是示例数据。`
        : '这 7 个模块的数据均为示例数据。'}</div>`);

  box.querySelectorAll('[data-go]').forEach((n) => n.addEventListener('click', () => {
    location.hash = '#/' + n.dataset.go;
  }));

  bindDecisionButtons(box);
}

/* 价格梯子：用最低/最高价做坐标轴 */
function ladderHtml(stats, myPrice) {
  if (!stats || !stats.count) return '<div class="note">无数据</div>';
  const lo = stats.min;
  const hi = stats.max;
  const span = Math.max(1, hi - lo);
  const pos = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  const marks = [stats.min, stats.p25, stats.median, stats.p75, stats.max];
  return `
    <div class="ladder">
      <div class="ladder-track"></div>
      ${marks.map((v) => `<div class="ladder-dot" style="left:${pos(v)}%" title="${money(v)}"></div>`).join('')}
      ${myPrice ? `<div class="ladder-me" style="left:${pos(myPrice)}%">
        <div class="flag">我 ${money(myPrice)}</div>
        <div class="pin"></div>
      </div>` : ''}
    </div>
    <div class="ladder-scale">
      <span>${money(lo)}</span><span>P25 ${money(stats.p25)}</span>
      <span>中位 ${money(stats.median)}</span><span>P75 ${money(stats.p75)}</span><span>${money(hi)}</span>
    </div>`;
}

/* 调价建议卡（概览与建议页共用） */
function decisionCard(s, opts) {
  const compact = opts && opts.compact;
  const cur = s.current_price;
  const next = s.suggested_price;
  const chg = next - cur;
  const chgPct = cur ? (chg / cur) * 100 : 0;
  const down = chg < 0;

  const chips = [];
  if (s.peers_count != null) chips.push(`<span class="chip">同档 <b>${s.peers_count}</b> 家</span>`);
  if (s.peers_median != null) chips.push(`<span class="chip">同档中位 <b>${money(s.peers_median)}</b></span>`);
  if (s.ideal_price != null) chips.push(`<span class="chip warm">理论目标 <b>${money(s.ideal_price)}</b></span>`);
  if (s.stepped) chips.push('<span class="chip">触发单次幅度上限</span>');
  if (s.fell_back) chips.push('<span class="chip warm">同档样本不足，参考全量</span>');
  if (s.occupancy_rate != null) chips.push(`<span class="chip">入住率 <b>${num(s.occupancy_rate, 0)}%</b></span>`);

  const id = s.id;
  const status = s.status || 'pending';
  let statusBadge;
  if (status === 'simulated') {
    statusBadge = '<span class="badge brand"><i></i>试算结果 · 不写库</span>';
  } else if (status === 'approved') {
    statusBadge = '<span class="badge live"><i></i>已确认执行</span>';
  } else if (status === 'rejected') {
    statusBadge = '<span class="badge static"><i></i>已决定不执行</span>';
  } else {
    statusBadge = '<span class="badge warm"><i></i>待拍板</span>';
  }

  const btnRow = (status === 'pending' && id) ? `
    <div class="row" style="margin-top:4px">
      <button class="btn accent" data-approve="${id}">确认按此价执行</button>
      <button class="btn" data-reject="${id}">不执行</button>
      <input type="text" data-note="${id}" placeholder="备注（可选，会记进调价记录）" style="flex:1;min-width:180px;max-width:320px">
    </div>` : '';

  return `<div class="decision ${status === 'pending' ? 'pending' : ''}">
    <div class="d-top">
      <div>
        <div class="d-title">${esc(s.date || '')} · 建议价</div>
      </div>
      ${statusBadge}
    </div>
    <div class="price-line">
      <span class="from">${money(cur)}</span>
      <span class="arrow">→</span>
      <span class="to">${money(next)}</span>
      <span class="chg ${down ? 'down' : ''}">${chg >= 0 ? '+' : ''}${Math.round(chg)} 元（${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(1)}%）</span>
    </div>
    ${s.reason ? `<div class="d-reason">${esc(s.reason)}</div>` : ''}
    ${chips.length ? `<div class="chips">${chips.join('')}</div>` : ''}
    ${!compact ? `<div class="note">确认后只写入本地调价记录，不会真的去改 OTA 后台价格——真实改价需要商家后台权限，本系统不碰。</div>` : ''}
    ${btnRow}
  </div>`;
}

function bindDecisionButtons(root) {
  root.querySelectorAll('[data-approve]').forEach((b) => {
    b.addEventListener('click', () => decide(b.dataset.approve, 'approved', root));
  });
  root.querySelectorAll('[data-reject]').forEach((b) => {
    b.addEventListener('click', () => decide(b.dataset.reject, 'rejected', root));
  });
}

async function decide(id, decision, root) {
  const noteEl = root.querySelector(`[data-note="${id}"]`);
  const note = noteEl ? noteEl.value.trim() : '';
  try {
    await apiPost(`/api/suggestions/${id}/decision`, { decision, note });
    toast(decision === 'approved' ? '已确认，写入调价记录' : '已标记为不执行', 'ok');
    renderPage();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ==========================================================================
   页面：价格雷达
   ========================================================================== */
async function renderRadar(box) {
  const q = state.radarDate ? ('?date=' + encodeURIComponent(state.radarDate)) : '';
  const d = await apiGet('/api/radar' + q);

  if (d.empty) {
    box.innerHTML = `<div class="card"><div class="empty"><b>还没有竞品数据</b>${esc(d.message)}</div></div>`;
    return;
  }
  state.radarDate = d.date;

  const my = d.myPrice;
  const stats = d.stats;

  const histMax = Math.max.apply(null, d.histogram.map((h) => h.count).concat([1]));

  const competitorRows = d.competitors.map((c, i) => {
    const delta = my ? c.price - my : null;
    const cls = delta == null ? '' : (delta < 0 ? 'ok' : 'warm');
    const label = delta == null ? '—' : (delta === 0 ? '持平' : (delta < 0 ? `便宜 ${Math.abs(delta).toFixed(0)}` : `贵 ${delta.toFixed(0)}`));
    return `<tr>
      <td class="muted">${i + 1}</td>
      <td class="name">${esc(c.name)}</td>
      <td class="r">${money(c.price)}</td>
      <td class="r ${cls}">${label}</td>
      <td class="r">${num(c.distance_km, 2)}</td>
      <td class="r">${c.rating == null ? '—' : Number(c.rating).toFixed(1)}</td>
      <td class="r muted">${c.reviews == null ? '—' : Number(c.reviews).toLocaleString('zh-CN')}</td>
    </tr>`;
  }).join('');

  box.innerHTML = `
    <div class="srcbar live"><b>实时数据</b>
      <span>${esc(d.date)} 携程采集 · 半径 ${esc(d.distanceKm)} km · 校验通过 ${stats.count} 家（价格 &gt; 0、按名称去重、城市一致性）</span>
      <span class="spacer"></span>
      <select id="dateSel" style="width:auto">${d.dates.map((x) => `<option ${x === d.date ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>
    </div>

    ${kpiGrid([
      { label: '竞品数量', value: stats.count + ' 家', sub: `最远 ${d.competitors.length ? num(Math.max.apply(null, d.competitors.map((c) => c.distance_km || 0)), 2) : '—'} km` },
      { label: '最低价', value: money(stats.min), sub: '库内最低，仅作参照' },
      { label: '中位价', value: money(stats.median), sub: '算法依据', tone: 'ok' },
      { label: '最高价', value: money(stats.max), sub: '库内最高，仅作参照' },
      { label: '平均价', value: money(stats.avg), sub: '易被高价拉偏，仅参考' },
      { label: 'P25–P75', value: money(stats.p25) + '–' + money(stats.p75), sub: '中间一半的区间' },
      { label: 'P90', value: money(stats.p90), sub: '高分位' },
      { label: '比我便宜', value: d.cheaperCount + ' 家', sub: `比我贵 ${d.expensiveCount} 家`, tone: 'warm' },
    ])}

    <div class="grid side-main">
      <div class="card">
        <div class="card-head"><b class="card-title">价格分布</b>
          <span class="card-meta">中位 ${money(stats.median)} · 平均 ${money(stats.avg)}</span></div>
        <div class="hist">
          ${d.histogram.map((h) => `
            <div class="hcol" title="${h.label} 元：${h.count} 家">
              <div class="hcount">${h.count || ''}</div>
              <div class="hbar" style="height:${(h.count / histMax) * 100}%"></div>
              <div class="hlabel">${h.label}</div>
            </div>`).join('')}
        </div>
        <div class="hist-legend">
          <span>我方 ${money(my)}</span>
          ${d.position ? `<span>排名 第 ${d.position.rank} / ${d.position.total} 位 · ${esc(d.position.position)}</span>` : ''}
          <span>区间单位：元</span>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><b class="card-title">同档分层</b>
          <span class="card-meta">当前价 × 0.6 ~ 1.6</span></div>
        ${d.bands.map((b) => `
          <div class="link-row">
            <span class="lk">${esc(b.label)}${b.range ? ` <span class="note">（${money(b.range[0])}–${money(b.range[1])}）</span>` : ''}</span>
            <span class="rv"><b>${b.count}</b> 家 · 中位 ${money(b.median)}</span>
          </div>`).join('')}
        <div class="note" style="margin-top:10px">算法只跟同档竞品比。把 ¥63 的客栈和 ¥697 的套房放一起平均，没有业务含义。</div>
      </div>
    </div>

    ${card('我方位置（竞品价格轴）', ladderHtml(stats, my), `当前价 ${money(my)}`)}

    <div class="card">
      <div class="card-head"><b class="card-title">What-if 试算</b>
        <span class="card-meta">用真实竞品数据实时重算，不写库</span></div>
      <div class="grid c2" style="gap:18px">
        <div class="field">
          <label>入住率 <span class="val" id="occVal">${num(d.occupancyRate || 45, 0)}%</span></label>
          <input type="range" id="occRange" min="0" max="100" step="1" value="${Math.round(d.occupancyRate || 45)}">
          <div class="note">低于 30% 走降价、高于 60% 走溢价、中间跟随</div>
        </div>
        <div class="field">
          <label>当前价（元）</label>
          <input type="number" id="priceInput" value="${Math.round(my || 0)}" step="1">
          <div class="note">改这个值可以试算「如果我挂另一个价，系统会怎么建议」</div>
        </div>
      </div>
      <div id="simResult" style="margin-top:16px"></div>
    </div>

    <div class="card">
      <div class="card-head"><b class="card-title">竞品明细</b>
        <span class="card-meta">${stats.count} 家 · 按价格升序</span></div>
      <div class="tbl-wrap">
        <table class="tbl"><thead><tr>
          <th>#</th><th>酒店</th><th class="r">价格</th><th class="r">与我相比</th>
          <th class="r">距离 km</th><th class="r">评分</th><th class="r">点评数</th>
        </tr></thead><tbody>${competitorRows}</tbody></table>
      </div>
    </div>
  `;

  $('#dateSel').addEventListener('change', (e) => {
    state.radarDate = e.target.value;
    renderPage();
  });

  const occRange = $('#occRange');
  const priceInput = $('#priceInput');
  let timer = null;
  const run = () => {
    $('#occVal').textContent = occRange.value + '%';
    clearTimeout(timer);
    timer = setTimeout(() => simulate(occRange.value, priceInput.value), 220);
  };
  occRange.addEventListener('input', run);
  priceInput.addEventListener('input', run);
  simulate(occRange.value, priceInput.value);
}

async function simulate(occ, price) {
  const target = $('#simResult');
  if (!target) return;
  target.innerHTML = '<div class="note">计算中…</div>';
  try {
    const d = await apiGet(`/api/analysis?occupancy=${encodeURIComponent(occ)}&price=${encodeURIComponent(price)}`);
    const s = d.detail;
    target.innerHTML = `
      ${decisionCard({
        date: d.date,
        current_price: s.current_price,
        suggested_price: s.suggested_price,
        reason: s.reason,
        occupancy_rate: s.occupancy_rate,
        peers_count: s.peers_count,
        peers_median: s.peers_median,
        ideal_price: s.ideal_price,
        stepped: s.stepped,
        fell_back: s.fell_back,
        status: 'simulated',
      }, { compact: true })}
      <div class="grid c3" style="margin-top:14px">
        ${d.bands.map((b) => `<div class="kpi ${b.occupancy === 45 ? '' : ''}">
          <div class="k-label">入住率 ${b.occupancy}%${b.occupancy === 45 ? '（当前档）' : ''}</div>
          <div class="k-num">${money(b.suggested)}</div>
          <div class="k-sub">理论目标 ${money(b.ideal)}${b.stepped ? ' · 分步靠近' : ''}</div>
        </div>`).join('')}
      </div>
      <div class="note" style="margin-top:10px">三档的「理论目标」不同；如果都显示同一个建议价，是因为目标价偏离当前价太远，被单次 15% 的幅度上限压住了。</div>
    `;
  } catch (err) {
    target.innerHTML = `<div class="note">试算失败：${esc(err.message)}</div>`;
  }
}

/* ==========================================================================
   页面：调价建议
   ========================================================================== */
async function renderSuggestions(box) {
  const [d, analysis] = await Promise.all([
    apiGet('/api/suggestions?limit=30'),
    apiGet('/api/analysis').catch(() => null),
  ]);

  const detail = analysis ? analysis.detail : null;
  const pending = d.suggestions.filter((s) => s.status === 'pending');
  const history = d.suggestions.filter((s) => s.status !== 'pending');

  const pendingHtml = pending.length
    ? pending.map((s) => {
      const merged = Object.assign({}, s);
      if (detail && detail.suggested_price === s.suggested_price) {
        Object.assign(merged, {
          peers_count: detail.peers_count,
          peers_median: detail.peers_median,
          ideal_price: detail.ideal_price,
          stepped: detail.stepped,
          fell_back: detail.fell_back,
        });
      }
      return decisionCard(merged);
    }).join('')
    : `<div class="empty"><b>没有待拍板的建议</b>跑一次采集后，系统会生成当日建议。</div>`;

  const historyRows = history.length ? history.map((s) => `
    <tr>
      <td>${esc(s.date)}</td>
      <td class="r">${money(s.current_price)}</td>
      <td class="r">${money(s.suggested_price)}</td>
      <td class="r ${s.suggested_price < s.current_price ? 'ok' : 'warm'}">${(s.suggested_price - s.current_price >= 0 ? '+' : '')}${Math.round(s.suggested_price - s.current_price)}</td>
      <td class="r">${num(s.occupancy_rate, 0)}%</td>
      <td>${s.status === 'approved' ? '<span class="ok">已确认</span>' : '<span class="muted">不执行</span>'}</td>
      <td class="muted">${esc(s.decision_note || '—')}</td>
      <td class="muted">${esc((s.decided_at || '').slice(0, 16))}</td>
    </tr>`).join('') : '<tr><td colspan="8" class="muted">暂无历史决策</td></tr>';

  const adjRows = d.adjustments.length ? d.adjustments.map((a) => `
    <tr>
      <td>${esc(a.date)}</td>
      <td class="r">${money(a.old_price)}</td>
      <td class="r">${money(a.new_price)}</td>
      <td class="r ${a.new_price < a.old_price ? 'ok' : 'warm'}">${(a.new_price - a.old_price >= 0 ? '+' : '')}${Math.round(a.new_price - a.old_price)}</td>
      <td class="muted">${esc(a.decided_by || '店长')}</td>
      <td class="muted">${esc((a.created_at || '').slice(0, 16))}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="muted">还没有已执行的调价记录</td></tr>';

  box.innerHTML = `
    <div class="srcbar live"><b>建议模式</b>
      <span>系统只出建议，改成「已确认」才算数。确认动作只写入本地调价记录，不会代替你去 OTA 后台改价。</span>
    </div>

    <div class="card">
      <div class="card-head"><b class="card-title">待我拍板（${pending.length}）</b>
        <span class="card-meta">${pending.length ? '改动幅度、理论目标价、同档样本都在卡里' : ''}</span></div>
      <div style="display:flex;flex-direction:column;gap:14px">${pendingHtml}</div>
    </div>

    ${card('历史决策', `<div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>日期</th><th class="r">当时价</th><th class="r">建议价</th><th class="r">差额</th>
        <th class="r">入住率</th><th>结论</th><th>备注</th><th>拍板时间</th>
      </tr></thead><tbody>${historyRows}</tbody></table></div>`)}

    ${card('调价记录（price_adjustments）', `<div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>日期</th><th class="r">原价</th><th class="r">新价</th><th class="r">差额</th><th>执行人</th><th>时间</th>
      </tr></thead><tbody>${adjRows}</tbody></table></div>
      <div class="note" style="margin-top:8px">这张表在「建议模式」下一直没人写过——缺的就是一个确认入口。现在点上面的按钮就会写一条。</div>`)}
  `;

  bindDecisionButtons(box);
}

/* ==========================================================================
   页面：差评回复
   ========================================================================== */
const REPLY_STATUS = {
  draft: { cls: 'warm', text: '草稿待确认' },
  used: { cls: 'live', text: '已采用' },
  discarded: { cls: 'static', text: '已弃用' },
};

/* 「待处理」= OTA 上还没有商家回复、系统里也没结案（未采用/弃用）的差评。
   「历史」= 商家已回复且已结案：流程走完了，默认收起，不挤占注意力。 */
const isPendingReply = (it) => !it.alreadyReplied && it.status === 'draft';
const isArchivedReply = (it) => !!it.alreadyReplied
  && (it.status === 'used' || it.status === 'discarded');

/* 按当前状态视图过滤条目（主题过滤在 paintReplyList 里单独做） */
function scopedReplyItems() {
  const st = state.replies;
  const all = st.data.items;
  const f = st.statusFilter;
  if (f === 'pending') return all.filter(isPendingReply);
  if (f === 'used') return all.filter((i) => i.status === 'used');
  if (f === 'discarded') return all.filter((i) => i.status === 'discarded');
  return all;
}

async function renderReplies(box) {
  const d = await apiGet('/api/replies');
  if (!d.available) {
    box.innerHTML = `<div class="card"><div class="empty"><b>还没有可用的点评快照</b>${esc(d.reason || '先采集一次点评')}</div></div>`;
    return;
  }

  state.replies = {
    data: d, statusFilter: 'pending', themeFilter: 'all',
    expanded: false, archiveExpanded: false,
  };

  const llmBar = d.llm.available
    ? `<span class="badge live"><i></i>模型可用 · ${esc(d.llm.model)}</span>`
    : `<span class="badge static"><i></i>模板模式</span>`;

  const srcBar = `<div class="srcbar snap">
    <b>真实点评快照</b>
    <span>${d.badTotal} 条差评（最新 ${esc(d.snapshotDate)}）。草稿由系统生成，<b>不会自动发布</b>——
      复制到携程商家后台发出后，回来点「标记已采用」。</span>
    <span class="srcact">
      ${llmBar}
      <button class="btn" id="btnRepliesCollect" style="white-space:nowrap">重新采集点评</button>
    </span>
  </div>`;

  const pendingCount = d.items.filter(isPendingReply).length;
  const kpis = [
    { label: '差评总数', value: `${d.badTotal} 条`, sub: `快照 ${esc(d.snapshotDate)}`, tone: 'warm' },
    { label: '已采用', value: `${d.stats.used} 条`, sub: '已发布到平台', tone: d.stats.used ? 'ok' : '' },
    { label: '已弃用', value: `${d.stats.discarded} 条`, sub: '决定不回复' },
    { label: '待处理', value: `${pendingCount} 条`,
      sub: `OTA 未回复 · 已落库 ${d.stats.savedTotal} 条草稿`, tone: 'warm' },
  ];

  box.innerHTML = srcBar + kpiGrid(kpis)
    + `<div class="card" style="margin-bottom:16px">
         <div class="card-head"><b class="card-title">按状态筛</b>
           <span class="card-meta">「待处理」= OTA 未回复且未结案，默认显示</span></div>
         <div class="chips" id="replyStatusFilters"></div>
         <div class="card-head" style="margin-top:12px"><b class="card-title">按主题筛</b>
           <span class="card-meta">一条差评可命中多个主题 · 计数跟随当前状态视图</span></div>
         <div class="chips" id="replyFilters"></div>
         <div class="note" style="margin-top:10px">${d.llm.available
           ? '「AI 重写」会调用已配置的模型；输出若没有预留整改占位符会被拒绝并回退模板。'
           : '未配置模型（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），当前全部走模板。模板稿同样可用，只是措辞更固定。'}</div>
       </div>
       <div id="replyList"></div>`;

  paintReplyList();
  paintReplyStatusFilters();
  paintReplyThemeFilters();

  const btn = $('#btnRepliesCollect');
  if (btn) btn.addEventListener('click', () => startCollect('reviews'));
  bindReplyActions(box);
}

function paintReplyStatusFilters() {
  const st = state.replies;
  const box = $('#replyStatusFilters');
  if (!st || !box) return;
  const items = st.data.items;
  const counts = {
    all: items.length,
    pending: items.filter(isPendingReply).length,
    used: items.filter((i) => i.status === 'used').length,
    discarded: items.filter((i) => i.status === 'discarded').length,
  };
  const defs = [
    { key: 'all', label: '全部' },
    { key: 'pending', label: '待处理' },
    { key: 'used', label: '已采用' },
    { key: 'discarded', label: '已弃用' },
  ];
  box.innerHTML = defs.map((f) => `
    <button class="chip ${st.statusFilter === f.key ? 'on' : ''}" data-status-filter="${f.key}">
      ${esc(f.label)} <b>${counts[f.key]}</b></button>`).join('');

  box.querySelectorAll('[data-status-filter]').forEach((b) => {
    b.addEventListener('click', () => {
      state.replies.statusFilter = b.dataset.statusFilter;
      state.replies.expanded = false;
      paintReplyStatusFilters();
      paintReplyThemeFilters();
      paintReplyList();
    });
  });
}

function paintReplyThemeFilters() {
  const st = state.replies;
  const box = $('#replyFilters');
  if (!st || !box) return;
  const scoped = scopedReplyItems();
  const counts = { all: scoped.length };
  scoped.forEach((i) => i.themes.forEach((t) => { counts[t] = (counts[t] || 0) + 1; }));
  const keys = ['all'].concat(st.data.themeCounts.map((t) => t.label));
  box.innerHTML = keys.map((k) => {
    const label = k === 'all' ? '全部' : k;
    const active = st.themeFilter === k;
    return `<button class="chip ${active ? 'on' : ''}" data-filter="${esc(k)}">
      ${esc(label)} <b>${counts[k] || 0}</b></button>`;
  }).join('');

  box.querySelectorAll('[data-filter]').forEach((b) => {
    b.addEventListener('click', () => {
      state.replies.themeFilter = b.dataset.filter;
      state.replies.expanded = false;
      paintReplyList();
    });
  });
}

function paintReplyList() {
  const st = state.replies;
  const list = $('#replyList');
  if (!st || !list) return;

  let items = scopedReplyItems();
  if (st.themeFilter !== 'all') items = items.filter((i) => i.themes.includes(st.themeFilter));

  // 「全部」视图：商家已回复且已结案（已采用/弃用）的归入历史区，默认收起
  let archived = [];
  if (st.statusFilter === 'all') {
    archived = items.filter(isArchivedReply);
    items = items.filter((i) => !isArchivedReply(i));
  }

  if (!items.length && !archived.length) {
    list.innerHTML = `<div class="card"><div class="empty"><b>${esc(replyEmptyTitle())}</b>${esc(replyEmptyHint())}</div></div>`;
    return;
  }

  const shown = st.expanded ? items : items.slice(0, 8);
  const more = items.length - shown.length;

  list.innerHTML = shown.map(replyCard).join('')
    + (more > 0
      ? `<div class="row" style="justify-content:center;margin:4px 0 16px">
           <button class="btn" id="btnMoreReplies">还有 ${more} 条，全部展开</button></div>`
      : '')
    + (archived.length
      ? (st.archiveExpanded
        ? `<div class="row" style="justify-content:center;margin:4px 0 8px">
             <button class="btn" id="btnReplyArchive">收起历史（已回复并结案 ${archived.length} 条）</button></div>`
          + archived.map(replyCard).join('')
        : `<div class="row" style="justify-content:center;margin:4px 0 16px">
             <button class="btn" id="btnReplyArchive">已回复并结案 ${archived.length} 条 · 展开查看</button></div>`)
      : '');

  const moreBtn = $('#btnMoreReplies');
  if (moreBtn) {
    moreBtn.addEventListener('click', () => {
      state.replies.expanded = true;
      paintReplyList();
      bindReplyActions($('#content'));
    });
  }
  const archiveBtn = $('#btnReplyArchive');
  if (archiveBtn) {
    archiveBtn.addEventListener('click', () => {
      state.replies.archiveExpanded = !state.replies.archiveExpanded;
      paintReplyList();
    });
  }
  autoGrowReplyTexts();
}

function replyEmptyTitle() {
  const st = state.replies;
  if (st.statusFilter === 'pending') return '没有待处理的差评';
  if (st.statusFilter === 'used') return '还没有标记为「已采用」的草稿';
  if (st.statusFilter === 'discarded') return '还没有「已弃用」的草稿';
  if (st.themeFilter !== 'all') return '这个状态与主题的组合下没有差评';
  return '这个主题下没有差评';
}

function replyEmptyHint() {
  const st = state.replies;
  if (st.statusFilter === 'pending') return 'OTA 未回复且未结案的差评为 0。切到「全部」可查看已回复与已结案的记录。';
  if (st.statusFilter === 'used') return '复制草稿到携程商家后台发出后，回来点「标记已采用」就会出现在这里。';
  if (st.statusFilter === 'discarded') return '决定不回复的草稿点「弃用」后归档在这里。';
  return '换一个筛选看看。';
}

/** 草稿正文都在 textarea 里，固定高度会截断内容——按实际内容撑开。 */
function autoGrowReplyTexts() {
  document.querySelectorAll('.reply-text').forEach((ta) => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight + 6, 560) + 'px';
  });
}

/** 标记采用/弃用后同步顶部计数，但不重绘列表（否则会丢掉展开与滚动位置）。 */
async function syncReplyStats() {
  try {
    const d = await apiGet('/api/replies');
    const nums = document.querySelectorAll('.kpi-grid .k-num');
    const subs = document.querySelectorAll('.kpi-grid .k-sub');
    if (nums.length < 4) return;
    nums[1].textContent = `${d.stats.used} 条`;
    nums[2].textContent = `${d.stats.discarded} 条`;
    nums[3].textContent = `${d.items.filter(isPendingReply).length} 条`;
    if (subs.length >= 4) subs[3].textContent = `OTA 未回复 · 已落库 ${d.stats.savedTotal} 条草稿`;
    if (state.replies) {
      /* 条目也要同步：切状态/主题筛选、历史折叠区都依赖最新的标记状态 */
      state.replies.data.items = d.items;
      state.replies.data.stats = d.stats;
      paintReplyStatusFilters();
      paintReplyThemeFilters();
    }
  } catch (e) { /* 计数同步失败不该打断操作 */ }
}

function replyCard(it) {
  const s = REPLY_STATUS[it.status] || REPLY_STATUS.draft;
  const warn = (it.warnings && it.warnings.length)
    ? it.warnings.map((w) => `<div class="reply-warn">⚠ ${esc(w)}</div>`).join('')
    : '';
  const skipped = (() => {
    const parts = [];
    (it.droppedThemes || []).forEach((t) => parts.push(`${t}（归类可能误判，未展开）`));
    (it.skippedThemes || []).forEach((t) => parts.push(t));
    return parts.length
      ? `<span class="reply-tag muted">未在回复中展开：${esc(parts.join('、'))}</span>`
      : '';
  })();
  const ph = it.placeholders.map((p) =>
    `<li><b>{{${esc(p.key)}}}</b> · ${esc(p.label)}
      <div class="note">建议措辞：${esc(p.example)}</div>
      <div class="note">${esc(p.note)}</div></li>`).join('');
  const checks = it.checklist.map((c) => `<li>${esc(c)}</li>`).join('');

  return `<div class="card reply-card" data-key="${esc(it.key)}" data-source="${esc(it.source)}">
    <div class="reply-head">
      <span class="badge ${it.rating <= 2 ? 'warm' : ''}">${num(it.rating, 1)} 分</span>
      <span class="muted mono">${esc(it.date)}</span>
      <span class="muted">${esc(it.roomTier || it.room || '房型未标注')}</span>
      ${it.themes.map((t) => `<span class="badge">${esc(t)}</span>`).join('')}
      ${it.alreadyReplied ? '<span class="badge static" title="这条点评在 OTA 上已有商家回复，无需再写">商家已回复</span>' : ''}
      <span class="reply-src mono" data-src>${it.source === 'llm' ? 'AI' : '模板'}</span>
      <span class="badge ${s.cls}" data-status>${s.text}</span>
    </div>
    <div class="reply-quote">${esc(it.content)}</div>
    ${warn}
    <textarea class="reply-text" data-draft spellcheck="false">${esc(it.text)}</textarea>
    <div class="row reply-actions">
      <button class="btn" data-act="copy">复制正文</button>
      <button class="btn" data-act="regen">重新生成模板稿</button>
      <button class="btn" data-act="llm">AI 重写</button>
      <button class="btn" data-act="save">保存修改</button>
      <button class="btn primary" data-act="used">标记已采用</button>
      <button class="btn" data-act="discard">弃用</button>
      <span class="note" data-hint style="margin-left:auto">${it.wordCount} 字 · ${esc(it.tone)}${it.edited ? ' · 已人工修改' : ''}</span>
    </div>
    <details class="reply-details">
      <summary>占位符与发布前自查（${it.placeholders.length} 项占位 · ${it.checklist.length} 条自查）</summary>
      ${skipped}
      <ul class="reply-ph">${ph}</ul>
      <div class="note" style="margin-top:8px">发布前逐条确认：</div>
      <ol class="reply-ph">${checks}</ol>
    </details>
  </div>`;
}

function bindReplyActions(box) {
  const list = $('#replyList', box) || $('#replyList');
  if (!list || list.dataset.bound === '1') return;
  list.dataset.bound = '1';

  list.addEventListener('input', (e) => {
    if (e.target.matches('[data-draft]')) {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight + 6, 560) + 'px';
    }
  });

  list.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const card = btn.closest('[data-key]');
    if (!card) return;
    const key = card.dataset.key;
    const ta = $('[data-draft]', card);
    const hint = $('[data-hint]', card);
    const act = btn.dataset.act;

    const busy = (on) => {
      btn.disabled = on;
      btn.textContent = on ? '处理中…' : btn.dataset.label || btn.textContent;
    };

    try {
      if (act === 'copy') {
        await copyText(ta.value);
        toast('正文已复制，粘贴到携程商家后台即可', 'ok');
        return;
      }

      if (act === 'regen' || act === 'llm') {
        btn.dataset.label = btn.textContent;
        busy(true);
        const r = await apiPost('/api/replies/generate', {
          key, preferLlm: act === 'llm',
        });
        ta.value = r.draft.text;
        ta.style.height = 'auto';
        ta.style.height = Math.min(ta.scrollHeight + 6, 560) + 'px';
        card.dataset.source = r.source;
        $('[data-src]', card).textContent = r.source === 'llm' ? 'AI' : '模板';
        if (r.fallbackReason) toast(r.fallbackReason, 'err');
        else toast(r.source === 'llm' ? '模型稿已生成' : '模板稿已重新生成', 'ok');
        if (hint) hint.textContent = ta.value.replace(/\s/g, '').length + ' 字';
        return;
      }

      if (act === 'save') {
        busy(true);
        await apiPost('/api/replies/save', { key, text: ta.value });
        toast('修改已保存', 'ok');
        if (hint) hint.textContent = ta.value.replace(/\s/g, '').length + ' 字 · 已人工修改';
        return;
      }

      if (act === 'used' || act === 'discard') {
        busy(true);
        const r = await apiPost('/api/replies/status', {
          key, status: act === 'used' ? 'used' : 'discarded',
        });
        const s = REPLY_STATUS[r.draft.status] || REPLY_STATUS.draft;
        const badge = $('[data-status]', card);
        badge.className = 'badge ' + s.cls;
        badge.textContent = s.text;
        toast(act === 'used' ? '已标记为采用' : '已弃用这条草稿', 'ok');
        syncReplyStats();
        return;
      }
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      if (act !== 'copy') {
        btn.disabled = false;
        if (btn.dataset.label) btn.textContent = btn.dataset.label;
      }
    }
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // 非 https / 无权限时剪贴板 API 不可用，退回选中 + execCommand
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
}

/* ==========================================================================
   页面：静态模块（schema 渲染）
   ========================================================================== */
async function renderStatic(box, key) {
  const m = await apiGet('/api/modules/' + key);
  const isSnapshot = m.source === 'snapshot';
  const bar = isSnapshot
    ? `<div class="srcbar snap">
         <b>历史快照</b>
         <span>${esc(m.statusNote || '数据来自历史采集快照')}</span>
         <button class="btn" id="btnReviewsCollect" style="margin-left:auto;white-space:nowrap">重新采集点评</button>
       </div>`
    : `<div class="srcbar">
         <b>示例数据</b>
         <span>${esc(m.statusNote || '该模块尚未实现，页面为示例数据')}。结构与交互已定，接真实数据时前端不用改。</span>
       </div>`;
  box.innerHTML = bar + renderBlocks(m.blocks);

  const btn = $('#btnReviewsCollect');
  if (btn) btn.addEventListener('click', () => startCollect('reviews'));
}

/* ==========================================================================
   页面：系统状态
   ========================================================================== */
async function renderSystem(box) {
  const s = await apiGet('/api/status');
  const cfg = s.config;
  const c = s.database.counts;
  const sc = s.scraper;
  const st = s.strategy;
  const rv = s.reviews || {};

  const flag = (ok, text) => `<span class="status-dot ${ok ? 'ok' : 'bad'}"></span> ${esc(text)}`;

  const d = s.daily || {};
  const dailyCell = (t) => {
    if (!d.enabled) return '<span class="muted">未开启</span>';
    if (!t) return '<span class="muted">今天还没有记录</span>';
    if (t.status === 'done' || t.done) return flag(true, `今日已采${t.detail ? '（' + esc(t.detail) + '）' : ''}${t.updatedAt ? ' · ' + esc(String(t.updatedAt).slice(11)) : ''}`);
    if (t.status === 'failed') return flag(false, `今日第 ${t.attempts} 次尝试失败${t.detail ? ' · ' + esc(t.detail).slice(0, 80) : ''}`);
    return '<span class="muted">今天还没有记录</span>';
  };

  const jobs = (s.jobs || []).map((j) => `
    <div class="link-row">
      <span class="lk">#${esc(j.id)} · ${esc(j.started)}</span>
      <span class="rv ${j.state === 'failed' ? 'warm' : (j.state === 'done' ? 'ok' : '')}">${esc(j.state)}${j.error ? ' · ' + esc(j.error).slice(0, 60) : ''}</span>
    </div>`).join('') || '<div class="note">还没有采集任务</div>';

  box.innerHTML = `
    <div class="srcbar ${cfg.ok ? 'live' : ''}">
      <b>${cfg.ok ? '配置就绪' : '配置不完整'}</b>
      <span>${cfg.ok ? '可以运行采集与定价计算。' : esc(cfg.message)}</span>
    </div>

    <div class="grid side-main">
      <div class="card">
        <div class="card-head"><b class="card-title">运行环境</b><span class="card-meta">${esc(s.serverTime)}</span></div>
        <div class="kv"><span>酒店</span><span>${esc(s.hotel.name)}（${esc(s.hotel.city)}）</span></div>
        <div class="kv"><span>数据来源 DATA_SOURCE</span><span class="mono">${esc(s.dataSource)}</span></div>
        <div class="kv"><span>当前价 / 入住率</span><span>${money(s.hotel.currentPrice)} · ${num(s.hotel.occupancyRate, 0)}%</span></div>
        <div class="kv"><span>数据库</span><span class="mono">${esc(s.database.path)}</span></div>
        <div class="kv"><span>有数据的日期</span><span>${s.database.dates.length ? esc(s.database.dates.join('、')) : '（空）'}</span></div>
        <div class="kv"><span>采集脚本</span><span>${flag(sc.scriptExists, sc.scriptExists ? '存在' : '缺失')}</span></div>
        <div class="kv"><span>采集解释器</span><span>${flag(sc.pythonExists, sc.pythonExists ? '存在' : '缺失')}</span></div>
        <div class="kv"><span>浏览器 profile</span><span>${flag(sc.profileExists, sc.profileExists ? '存在（需已登录携程）' : '缺失')}</span></div>
        <div class="kv"><span>浏览器渠道 / 端口</span><span class="mono">${esc(sc.channel)} · ${esc(sc.timeout)}s 超时</span></div>
      </div>

      <div class="card">
        <div class="card-head"><b class="card-title">库内记录</b><span class="card-meta">同一天只保留一条</span></div>
        <div class="kv"><span>my_hotel_prices</span><span>${c.my_hotel_prices} 条</span></div>
        <div class="kv"><span>competitor_prices</span><span>${c.competitor_prices} 条</span></div>
        <div class="kv"><span>price_suggestions</span><span>${c.price_suggestions} 条</span></div>
        <div class="kv"><span>price_adjustments</span><span>${c.price_adjustments} 条</span></div>
        <div class="kv"><span>reply_drafts</span><span>${(s.replies && s.replies.total) || 0} 条</span></div>
        <div class="kv"><span>daily_collects</span><span>${c.daily_collects != null ? c.daily_collects + ' 条' : '—'}</span></div>
        <div class="note" style="margin-top:10px">price_adjustments 建库起一直是空的，因为「建议模式」缺人工确认入口；现在在「调价建议」页点确认就会写入。</div>
      </div>
    </div>

    ${card('定价策略（config.PRICING_STRATEGY）', `<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 26px">
      ${Object.keys(st).map((k) => `<div class="kv"><span class="mono">${esc(k)}</span><span><b>${esc(st[k] == null ? 'null' : st[k])}</b></span></div>`).join('')}
    </div>
    <div class="note" style="margin-top:10px">这些值在 .env / config.py 里改。入门调参建议：先动 max_step_ratio（单次幅度上限），再动三个 occupancy_ratio（各档偏移）。</div>`)}

    <div class="grid c2">
      <div style="display:flex;flex-direction:column;gap:16px">
        ${card('每日自动采集', `
          <div class="kv"><span>机制</span><span>${d.enabled
            ? `每天 ${esc(d.at)} 自动执行；到点后当天首次进入中台会立即触发；当天已采过（含手动）不再重复`
            : '未开启（.env 设 DAILY_COLLECT_ENABLED=true 恢复）'}</span></div>
          <div class="kv"><span>价格数据</span><span>${dailyCell(d.prices)}</span></div>
          <div class="kv"><span>点评数据</span><span>${dailyCell(d.reviews)}</span></div>
          ${d.skipReason ? `<div class="note">当前为 ${esc(d.dataSource)} 模式：${esc(d.skipReason)}。</div>` : ''}
          ${d.error ? `<div class="note">读取每日采集状态失败：${esc(d.error)}</div>` : ''}
          <div class="note">失败当天不自动重试，可点下方「立即采集」手动补采；判重记录在 daily_collects 表，重启中台不会重复采。</div>`)}
        ${card('价格采集', jobs + `<div class="row" style="margin-top:12px"><button class="btn primary" id="btnCollect2">立即采集一次</button>
          <span class="note">约 30–90 秒；跑完自动刷新数据</span></div>`)}
        ${card('点评采集（口碑 / 差评两页的数据源）', `
          <div class="kv"><span>采集脚本</span><span>${flag(rv.scriptExists, rv.script || '未配置')}</span></div>
          <div class="kv"><span>解释器（venv）</span><span>${flag(rv.pythonExists, rv.python || '未配置')}</span></div>
          <div class="kv"><span>浏览器 profile</span><span>${flag(rv.profileExists, rv.profile || '未配置')}</span></div>
          <div class="kv"><span>当前快照</span><span>${rv.snapshotExists
            ? `${Number(rv.snapshotCount || 0).toLocaleString('zh-CN')} 条 · 截至 ${esc(rv.snapshotDateMax || '—')} · 采集于 ${esc(rv.snapshotMtime || '')}`
            : '<span class="muted">尚未采集</span>'}</span></div>
          <div class="kv"><span>保留范围</span><span>发表时间早于 ${esc(rv.before || '不过滤')}</span></div>
          <div class="mono" style="margin-top:8px;color:var(--muted);word-break:break-all">${esc(rv.snapshot || '—')}</div>
          <div class="row" style="margin-top:12px">
            <button class="btn" id="btnReviewsCollect2">重新采集点评</button>
            <span class="note">1–3 分钟；会打开可见浏览器</span>
          </div>
          <div class="note" style="margin-top:8px">采集依赖已登录的 profile。若浏览器停在登录墙或滑块，需要你手动处理，任务最多等 ${esc(rv.timeout || '—')} 秒；超时或失败都不会覆盖已有快照。</div>`)}
        ${card('回复草稿与模型（可选增强）', `
          <div class="kv"><span>草稿总数</span><span>${(s.replies && s.replies.total) || 0} 条</span></div>
          <div class="kv"><span>待确认 / 已采用 / 已弃用</span><span>${(s.replies && s.replies.draft) || 0} / ${(s.replies && s.replies.used) || 0} / ${(s.replies && s.replies.discarded) || 0}</span></div>
          <div class="kv"><span>模型</span><span>${(s.llm && s.llm.available)
            ? flag(true, s.llm.model || '已配置')
            : `<span class="muted">未配置，走模板（${esc((s.llm && s.llm.reason) || '')}）</span>`}</span></div>
          <div class="kv"><span>LLM_BASE_URL</span><span class="mono">${esc((s.llm && s.llm.baseUrl) || '—')}</span></div>
          <div class="note" style="margin-top:8px">模型只用来写回复正文，不参与算价、不写库、不碰价格。未配置时全部走模板，功能不受影响；模型稿若没预留整改占位符会被拒绝并回退模板。</div>`)}
      </div>
      ${card('模块清单', `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>模块</th><th>状态</th><th>说明</th></tr></thead><tbody>
        ${s.modules.map((m) => `<tr><td class="name">${esc(m.title)}</td>
          <td>${m.status === 'live' ? '<span class="ok">实时</span>'
              : (m.status === 'snapshot' ? '<span class="warm">快照</span>'
              : '<span class="muted">示例</span>')}</td>
          <td class="muted">${esc(m.statusNote || '—')}</td></tr>`).join('')}
      </tbody></table></div>`)}
    </div>

    <div class="card">
      <div class="card-head"><b class="card-title">关于数据来源的说明</b></div>
      <div class="note">${esc((s.staticMeta && s.staticMeta.disclaimer) || '')}</div>
    </div>
  `;

  const b = $('#btnCollect2');
  if (b) b.addEventListener('click', () => startCollect('prices'));
  const br = $('#btnReviewsCollect2');
  if (br) br.addEventListener('click', () => startCollect('reviews'));
}

/* ==========================================================================
   采集任务
   ========================================================================== */
async function startCollect(kind) {
  const isRev = kind === 'reviews';
  const btn = isRev ? $('#btnReviewsCollect') : $('#btnCollect');
  if (btn) btn.disabled = true;
  try {
    const job = await apiPost(isRev ? '/api/reviews/collect' : '/api/collect', {});
    state.jobId = job.id;
    state.jobKind = kind || 'prices';
    showJobbar(job);
    pollJob();
  } catch (err) {
    toast(err.message, 'err');
    if (btn) btn.disabled = false;
  }
}

function showJobbar(job) {
  const bar = $('#jobbar');
  bar.classList.add('show');
  const isRev = (job.kind || state.jobKind) === 'reviews';
  $('#jbTitle').textContent = (isRev ? '点评采集 #' : '采集任务 #') + (job.id || '');
  paintJob(job);
}

function paintJob(job) {
  const running = job.state === 'running';
  const isRev = (job.kind || state.jobKind) === 'reviews';
  $('#jbSpin').style.display = running ? '' : 'none';
  const badge = $('#jbState');
  badge.className = 'badge ' + (job.state === 'done' ? 'live' : (job.state === 'failed' ? 'warm' : ''));
  badge.textContent = job.state;
  $('#jbLog').textContent = (job.log || []).join('\n');
  const body = $('.jb-body');
  if (body) body.scrollTop = body.scrollHeight;
  $('#jbHint').textContent = running
    ? (isRev
        ? '正在调用携程点评采集脚本。浏览器会自动打开，若停在登录/滑块页请手动处理，通常 1–3 分钟'
        : '正在调用携程采集脚本，通常需要 30–90 秒')
    : (job.state === 'done'
        ? (isRev ? '完成。点评快照已更新，页面指标随之变化' : '完成。数据已写入 pricing.db')
        : ('失败：' + (job.error || '')));
}

function pollJob() {
  clearTimeout(state.jobTimer);
  state.jobTimer = setTimeout(async () => {
    try {
      const job = await apiGet('/api/jobs/' + state.jobId);
      paintJob(job);
      if (job.state === 'running') {
        pollJob();
      } else {
        const isRev = (job.kind || state.jobKind) === 'reviews';
        ['#btnCollect', '#btnReviewsCollect', '#btnReviewsCollect2'].forEach((sel) => {
          const b = $(sel);
          if (b) b.disabled = false;
        });
        toast(
          job.state === 'done'
            ? (isRev ? '点评采集完成，口碑数据已更新' : '采集完成，数据已更新')
            : '采集失败，展开右下角看日志',
          job.state === 'done' ? 'ok' : 'err'
        );
        renderPage();
        loadStatus();
      }
    } catch (err) {
      ['#btnCollect', '#btnReviewsCollect', '#btnReviewsCollect2'].forEach((sel) => {
        const b = $(sel);
        if (b) b.disabled = false;
      });
      toast('任务状态读取失败：' + err.message, 'err');
    }
  }, 1500);
}

/* ==========================================================================
   启动
   ========================================================================== */
async function loadStatus() {
  try {
    const s = await apiGet('/api/status');
    state.status = s;
    $('#sideHotel').innerHTML = `<b>${esc(s.hotel.name)}</b>${esc(s.hotel.city || '')} · ${esc(s.dataSource)} 模式`;
    if (s.jobs && s.jobs.length && s.jobs[0].state === 'running' && !state.jobId) {
      state.jobId = s.jobs[0].id;
      state.jobKind = s.jobs[0].kind || 'prices';
      showJobbar(s.jobs[0]);
      $('#btnCollect').disabled = true;
      pollJob();
    }
  } catch (err) {
    $('#sideHotel').innerHTML = '<b>无法读取状态</b>服务未就绪';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('#btnRefresh').addEventListener('click', () => { renderPage(); loadStatus(); });
  $('#btnCollect').addEventListener('click', () => startCollect('prices'));
  $('#jbClose').addEventListener('click', () => $('#jobbar').classList.remove('show'));
  window.addEventListener('hashchange', route);
  if (!location.hash) location.hash = '#/overview';
  loadStatus();
  route();
});
