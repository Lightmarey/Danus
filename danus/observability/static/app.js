/* Danus dashboard client. Overview / Fact Graph (echarts DAG) / Global Memory
   (per-channel). Read-only; polls the FastAPI app in app.py. */
'use strict';
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? '' : String(s));

let controlToken = '';
if (location.hash.startsWith('#control-token=')) {
  controlToken = decodeURIComponent(location.hash.slice('#control-token='.length));
  history.replaceState(null, '', location.pathname + location.search);
}
async function api(path, options = {}) {
  const r = await fetch(path, options);
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}
function connError(on) { const b = $('#conn-banner'); if (b) b.hidden = !on; }

// ---- markdown + math ----------------------------------------------------- //
// elaboration / master_guidance / pro replies are markdown with LaTeX. Render
// markdown, but PROTECT math spans first so markdown-it doesn't mangle _ * { }.
let _md = null;
function md() {
  if (!_md && window.markdownit) _md = window.markdownit({ html: false, linkify: true, breaks: false });
  return _md;
}
function mdmath(node, text) {
  text = esc(text);
  node.classList.add('md');
  const m = md();
  const hasKatex = !!window.katex;
  const store = [];
  const stash = (tex, disp) => { store.push({ tex, disp }); return `@@MATH${store.length - 1}@@`; };
  let t = text;
  t = t.replace(/\\\[([\s\S]+?)\\\]/g, (_, x) => stash(x, true));
  t = t.replace(/\$\$([\s\S]+?)\$\$/g, (_, x) => stash(x, true));
  t = t.replace(/\\\(([\s\S]+?)\\\)/g, (_, x) => stash(x, false));
  t = t.replace(/\$([^\n$]+?)\$/g, (_, x) => stash(x, false));
  let html = m ? m.render(t) : t.replace(/\n/g, '<br>');
  html = html.replace(/@@MATH(\d+)@@/g, (_, i) => {
    const { tex, disp } = store[i];
    if (!hasKatex) return tex;
    try { return katex.renderToString(tex, { displayMode: disp, throwOnError: false }); }
    catch (e) { return esc(tex); }
  });
  node.innerHTML = html;
}

// ---- tabs ---------------------------------------------------------------- //
function switchTab(name) {
  document.querySelectorAll('.nav-link').forEach((a) => a.classList.toggle('active', a.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
  if (name === 'overview') loadOverview();
  if (name === 'graph') loadGraph();
  if (name === 'memory') loadMemory();
  if (name === 'control') loadControl();
  if (name === 'graph' && graphChart) setTimeout(() => graphChart.resize(), 50);
}
document.querySelectorAll('.nav-link').forEach((a) => (a.onclick = () => switchTab(a.dataset.tab)));

// ---- overview ------------------------------------------------------------ //
function bars(container, obj, colorFn) {
  container.innerHTML = '';
  const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map((e) => e[1]));
  for (const [k, v] of entries) {
    const row = el('div', 'bar-row');
    row.appendChild(el('div', 'bar-label', k));
    const track = el('div', 'bar-track');
    const fill = el('div', 'bar-fill');
    fill.style.width = (v / max * 100).toFixed(1) + '%';
    if (colorFn) fill.style.background = colorFn(k);
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'bar-val', String(v)));
    container.appendChild(row);
  }
}
async function loadOverview() {
  try {
    const d = await api('/api/overview');
    connError(false);
    $('#project-badge').textContent = d.project || '';
    $('#refresh-note').textContent = 'updated ' + new Date(d.updated_at * 1000).toLocaleTimeString();
    const cards = [
      ['Verified facts', d.facts, `${d.facts_with_predecessors} with predecessors`],
      ['Global-memory entries', Object.values(d.channel_counts).reduce((a, b) => a + b, 0), `${Object.keys(d.channel_counts).length} channels`],
      ['Verifier verdicts', (d.verdicts.correct || 0) + (d.verdicts.wrong || 0), `${d.verdicts.correct || 0} correct · ${d.verdicts.wrong || 0} wrong`],
      ['Pro consults', d.consult_count, `$${d.consult_cost_usd} spent`],
    ];
    const c = $('#ov-cards'); c.innerHTML = '';
    for (const [k, v, sub] of cards) {
      const card = el('div', 'card');
      card.appendChild(el('div', 'k', k));
      card.appendChild(el('div', 'v', String(v)));
      card.appendChild(el('div', 'sub', sub));
      c.appendChild(card);
    }
    bars($('#ov-channels'), d.channel_counts);
    bars($('#ov-authors'), d.facts_by_author);
    const vd = $('#ov-verdicts'); vd.innerHTML = '';
    const chips = el('div', 'chips');
    const vmap = { correct: 'var(--green)', wrong: 'var(--red)', error: 'var(--orange)' };
    for (const [k, v] of Object.entries(d.verdicts)) {
      const chip = el('div', 'chip');
      const dot = el('span', 'dot'); dot.style.background = vmap[k] || 'var(--text-muted)';
      chip.appendChild(dot); chip.appendChild(document.createTextNode(`${k}: ${v}`));
      chips.appendChild(chip);
    }
    vd.appendChild(chips);
  } catch (e) { connError(true); }
}

// ---- fact graph (echarts) ------------------------------------------------ //
let graphChart = null;
let factById = {};
// importance = dependency depth (longest path from an axiom/leaf up to a fact).
// Continuous shade: the deeper a fact, the darker — shallow=light, deep=dark.
function depthColor(depth, maxDepth) {
  const t = maxDepth > 0 ? Math.min(1, depth / maxDepth) : 0;
  return `hsl(248, ${(50 + t * 26).toFixed(0)}%, ${(80 - t * 52).toFixed(0)}%)`;
}
async function loadGraph() {
  try {
    const d = await api('/api/factgraph');
    connError(false);
    factById = {}; d.nodes.forEach((n) => (factById[n.id] = n));
    const maxD = d.max_depth || 1;
    const nodes = d.nodes.map((n) => {
      const dp = n.depth || 0;
      return {
        id: n.id, name: n.id.slice(0, 7),
        symbolSize: 6 + Math.min(16, dp * 2.5),        // bigger = deeper, but capped small so circles don't overlap when zoomed out
        itemStyle: { color: depthColor(dp, maxD) },
        depth: dp, author: n.author,                   // no per-node label (would clutter)
      };
    });
    const links = d.edges.map((e) => ({ source: e.source, target: e.target }));
    if (!graphChart) graphChart = echarts.init($('#graph'));
    graphChart.setOption({
      tooltip: {
        formatter: (p) => p.dataType === 'node'
          ? `<b>${p.data.id.slice(0, 10)}</b> · by ${p.data.author}<br/>dependency depth: ${p.data.depth} layer(s)<br/>${esc((factById[p.data.id] || {}).statement).slice(0, 100)}…`
          : '',
      },
      series: [{
        type: 'graph', layout: 'force', roam: true, draggable: true,
        force: { repulsion: 160, edgeLength: 80, gravity: 0.06 },
        label: { show: false }, emphasis: { focus: 'adjacency', label: { show: false } },
        lineStyle: { color: '#cbd5e1', width: 1, opacity: 0.55, curveness: 0.05 },
        edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 5,
        data: nodes, links: links,
      }],
    });
    graphChart.off('click');
    graphChart.on('click', (p) => { if (p.dataType === 'node') showFact(p.data.id); });
    $('#graph-stat').textContent = `${d.nodes.length} facts · ${d.edges.length} edges · max depth ${d.max_depth}`;
    graphChart.resize();
  } catch (e) { connError(true); }
}
function showFact(id) {
  const f = factById[id]; if (!f) return;
  const d = $('#fact-detail'); d.innerHTML = '';
  d.appendChild(el('div', 'fid', f.id));
  if (f.author || f.problem_id) d.appendChild(el('div', 'muted', `${f.author || '?'} · ${f.problem_id || ''}`));
  const addSec = (h, txt) => { if (!txt) return; d.appendChild(el('div', 'sec-h', h)); const m = el('div', 'math'); mdmath(m, txt); d.appendChild(m); };
  addSec('Statement', f.statement);
  addSec('Proof', f.proof);
  addSec('Intuition', f.intuition);
  if (f.predecessors.length) {
    d.appendChild(el('div', 'sec-h', `Predecessors (${f.predecessors.length})`));
    const wrap = el('div');
    f.predecessors.forEach((p) => { const a = el('span', 'pred-link', p.slice(0, 10)); a.onclick = () => showFact(p); wrap.appendChild(a); });
    d.appendChild(wrap);
  }
}

// ---- global memory ------------------------------------------------------- //
let memInit = false;
// channels whose entries are long markdown (summary/strategy) — render full, no clamp.
const LONGFORM = new Set(['elaboration', 'master_guidance', 'verification']);
async function loadMemory() {
  if (memInit) return; memInit = true;
  try {
    const d = await api('/api/channels');
    connError(false);
    const st = $('#mem-subtabs'); st.innerHTML = '';
    d.channels.forEach((ch, i) => {
      const b = el('div', 'subtab' + (i === 0 ? ' active' : ''));
      b.appendChild(document.createTextNode(ch.kind));
      b.appendChild(el('span', 'cnt', String(ch.count)));
      b.onclick = () => { document.querySelectorAll('.subtab').forEach((x) => x.classList.remove('active')); b.classList.add('active'); loadChannel(ch.kind); };
      st.appendChild(b);
    });
    if (d.channels.length) loadChannel(d.channels[0].kind);
  } catch (e) { connError(true); memInit = false; }
}
async function loadChannel(kind) {
  const list = $('#mem-list'); list.innerHTML = '<div class="empty">loading…</div>';
  const longform = LONGFORM.has(kind);
  try {
    const d = await api('/api/channel/' + kind);
    list.innerHTML = '';
    if (!d.entries.length) { list.innerHTML = '<div class="empty">no entries in this channel</div>'; return; }
    for (const e of d.entries) {
      const card = el('div', 'entry' + (longform ? ' longform' : ''));
      const head = el('div', 'entry-head');
      head.appendChild(el('span', 'entry-author', e.author || '?'));
      if (e.verdict) head.appendChild(el('span', 'tag ' + (e.verdict === 'correct' ? 'correct' : e.verdict === 'wrong' ? 'wrong' : ''), e.verdict));
      if (e.fact_id) head.appendChild(el('span', 'tag fid', String(e.fact_id).slice(0, 10)));
      if (e.cost_usd != null) head.appendChild(el('span', 'tag cost', '$' + e.cost_usd));
      if (e.status) head.appendChild(el('span', 'tag', e.status));
      head.appendChild(el('span', 'entry-ts', (e.timestamp_utc || '').slice(0, 19).replace('T', ' ')));
      card.appendChild(head);
      const claim = el('div', 'entry-claim'); mdmath(claim, e.claim); card.appendChild(claim);
      if (e.evidence) {
        const ev = el('div', 'entry-evidence' + (longform ? '' : ' clamp'));
        mdmath(ev, e.evidence); card.appendChild(ev);
        if (!longform) {
          const more = el('div', 'more', 'show more ▾');
          more.onclick = () => { const open = ev.classList.toggle('open'); more.textContent = open ? 'show less ▴' : 'show more ▾'; };
          card.appendChild(more);
        }
      }
      list.appendChild(card);
    }
  } catch (err) { list.innerHTML = '<div class="empty">failed to load</div>'; }
}

// ---- Danus v2 research control ----------------------------------------- //
const viewPins = new Set(); // deliberately ephemeral and never sent to the server
let routeChart = null;
let controlGeneration = 0;

async function controlPost(path, body) {
  if (!controlToken) throw new Error('This page has no control capability token. Reopen the launch URL.');
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Danus-Control-Token': controlToken }, body: JSON.stringify(body) });
}

function governanceCard(target) {
  const card = el('div', 'entry');
  const head = el('div', 'entry-head');
  head.appendChild(el('span', 'entry-author', target.version));
  head.appendChild(el('span', 'tag', target.state));
  card.appendChild(head);
  card.appendChild(el('div', 'entry-claim', target.statement || ''));
  if (target.diff) { const diff = el('pre', 'entry-evidence clamp', target.diff); card.appendChild(diff); }
  const actions = el('div', 'control-actions');
  if (target.state === 'draft') {
    const approve = el('button', '', 'Approve');
    approve.onclick = async () => {
      if (prompt(`Type ${target.version} to approve this target`) !== target.version) return;
      try { await controlPost(`/api/control/targets/${target.version}/approve`, { request_id: crypto.randomUUID(), expected_generation: controlGeneration }); await loadControl(); }
      catch (e) { alert(e.message); }
    };
    actions.appendChild(approve);
  }
  if (target.state === 'approved') {
    const withdraw = el('button', '', 'Withdraw');
    withdraw.onclick = async () => {
      const reason = prompt('Withdrawal reason (required)'); if (!reason) return;
      try { await controlPost(`/api/control/targets/${target.version}/withdraw`, { request_id: crypto.randomUUID(), expected_generation: controlGeneration, reason }); await loadControl(); }
      catch (e) { alert(e.message); }
    };
    actions.appendChild(withdraw);
  }
  if (actions.childNodes.length) card.appendChild(actions);
  return card;
}

function renderFactGraph(group) {
  const facts = group.facts || [];
  const nodeById = Object.fromEntries(facts.map((fact) => [fact.fact_id, fact]));
  if (!routeChart) routeChart = echarts.init($('#route-graph'));
  routeChart.setOption({
    tooltip: { formatter: (p) => p.dataType === 'node' ? `${esc(p.data.title)}<br>${p.data.role}${p.data.shared ? ' · shared' : ''}` : '' },
    series: [{ type:'graph', layout:'force', roam:true, force:{repulsion:170,edgeLength:75,gravity:.05},
      label:{show:true,position:'right',formatter:(p)=>p.data.title.slice(0,32)},
      data:facts.map((fact) => ({ id:fact.fact_id, name:fact.fact_id, title:fact.title, role:fact.role, shared:fact.shared,
        symbolSize:fact.role === 'closing' ? 19 : fact.role === 'direct' ? 15 : 11,
        itemStyle:{color:viewPins.has(fact.fact_id) ? '#ef7f1a' : fact.role === 'closing' ? '#16a34a' : fact.role === 'support' ? '#94a3b8' : '#6366f1'} })),
      links:(group.edges || []).filter((edge) => nodeById[edge.source] && nodeById[edge.target]),
      lineStyle:{color:'#cbd5e1'}, emphasis:{focus:'adjacency'} }]
  }, true);
  routeChart.off('click');
  routeChart.on('click', (p) => { if (p.dataType === 'node') showResearchFact(p.data.id); });
  $('#route-stat').textContent = `${facts.length} nodes · ${group.unexpanded_count || 0} unexpanded`;
}

async function showResearchFact(factId) {
  const detail = $('#research-detail'); detail.innerHTML = '<div class="empty">loading…</div>';
  try {
    const fact = await api(`/api/research/facts/${factId}?include_proof=true`); detail.innerHTML = '';
    const card = el('div', 'entry' + (viewPins.has(factId) ? ' pinned' : ''));
    card.appendChild(el('div', 'entry-author', fact.title));
    card.appendChild(el('div', 'tag fid', fact.fact_id));
    const statement = el('div', 'entry-claim'); mdmath(statement, fact.statement); card.appendChild(statement);
    const proof = el('div', 'entry-evidence'); mdmath(proof, fact.proof); card.appendChild(proof);
    const pin = el('button', '', viewPins.has(factId) ? 'Unpin from view' : 'Pin in view');
    pin.onclick = () => { viewPins.has(factId) ? viewPins.delete(factId) : viewPins.add(factId); showResearchFact(factId); };
    card.appendChild(pin); detail.appendChild(card);
  } catch (e) { detail.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function selectRoute(routeId) {
  try {
    const d = await api(`/api/research/routes/${routeId}?snapshot=${controlGeneration}`);
    renderFactGraph(d.fact_group);
    const detail = $('#research-detail'); detail.innerHTML = '';
    const route = el('div', 'entry'); route.appendChild(el('div', 'entry-author', d.route.method_title));
    route.appendChild(el('div', 'entry-claim', d.route.expected_result || ''));
    route.appendChild(el('div', 'gain-seq', `state ${d.route.state} · gains ${d.checkpoints.map((x)=>x.gain).reverse().join(' → ') || 'none'}`));
    d.obstacles.forEach((x) => route.appendChild(el('div', 'entry-evidence', `Obstacle: ${x.title} (${x.occurrences})`)));
    const recent = d.checkpoints[0] && d.checkpoints[0].report;
    if (recent && recent.unresolved_interfaces) route.appendChild(el('div', 'entry-evidence', `Unresolved: ${recent.unresolved_interfaces.join(', ')}`));
    if (recent && recent.recommended_next_action) route.appendChild(el('div', 'entry-evidence', `Next: ${recent.recommended_next_action}`));
    detail.appendChild(route);
  } catch (e) { connError(true); }
}

async function loadManifests() {
  const box = $('#context-manifests'); box.innerHTML = '';
  try {
    const d = await api('/api/research/context-manifests?limit=10');
    if (!d.manifests.length) { box.appendChild(el('div','empty','No slice snapshots yet.')); return; }
    d.manifests.forEach((m) => {
      const card = el('div','entry clickable');
      card.appendChild(el('div','entry-author',`${m.worker} · snapshot ${m.snapshot_generation}`));
      card.appendChild(el('div','entry-claim',`${m.facts.length} facts · ${m.compression.title_only_count} title-only · ${m.compression.unexpanded_count} unexpanded`));
      card.onclick = () => { const detail=$('#research-detail'); detail.innerHTML=''; m.facts.forEach((f)=>{ const row=el('div','entry clickable'); row.appendChild(el('div','entry-author',f.title)); row.appendChild(el('div','muted',`${f.mode} · ${f.reasons.join(', ')}`)); row.onclick=()=>showResearchFact(f.fact_id); detail.appendChild(row); }); };
      box.appendChild(card);
    });
  } catch (e) { box.appendChild(el('div','empty','Failed to load snapshots.')); }
}

async function loadControl() {
  try {
    const state = await api('/api/control');
    if (!state.enabled) { $('#control-summary').innerHTML='<div class="empty">Legacy project — v2 research control is not enabled.</div>'; return; }
    const d = await api('/api/research/map'); connError(false);
    const summary = $('#control-summary'); summary.innerHTML = '';
    controlGeneration = d.generation;
    const cards = [
      ['Current target', d.active_target ? d.active_target.version : 'none', `${d.targets.length} version(s)`],
      ['Obligation closure', `${d.obligations.filter((x)=>x.state==='closed').length}/${d.obligations.length}`, 'coverage, not a proof percentage'],
      ['Routes', d.methods.reduce((n,m)=>n+m.routes.length,0), `${d.methods.length} method(s)`],
      ['Budget', d.budget.stage, `${Math.round(d.budget.ratio * 100)}% of configured hard bound`],
    ];
    cards.forEach(([k, v, sub]) => { const c = el('div', 'card'); c.appendChild(el('div', 'k', k)); c.appendChild(el('div', 'v', String(v))); c.appendChild(el('div', 'sub', sub)); summary.appendChild(c); });
    const targets=$('#control-targets'); targets.innerHTML=''; d.targets.forEach((t)=>targets.appendChild(governanceCard(t)));
    const methods=$('#control-methods'); methods.innerHTML='';
    d.methods.forEach((method)=>{ const group=el('div','entry'); group.appendChild(el('div','entry-author',method.method_title)); method.routes.forEach((r)=>{ const row=el('div','entry-evidence clickable',`${r.id} · ${r.state} · ${r.obligation_id}`); row.onclick=()=>selectRoute(r.id); group.appendChild(row); }); methods.appendChild(group); });
    const obligations=$('#control-obligations'); obligations.innerHTML=''; d.obligations.forEach((o)=>{ const row=el('div','entry clickable'); row.appendChild(el('div','entry-author',o.id)); row.appendChild(el('span','tag',o.state)); row.appendChild(el('div','entry-claim',o.statement)); row.onclick=async()=>{ const x=await api(`/api/research/obligations/${o.id}?snapshot=${controlGeneration}`); renderFactGraph(x.fact_group); }; obligations.appendChild(row); });
    await loadManifests();
  } catch (e) { connError(true); }
}
$('#clear-pins').onclick = () => { viewPins.clear(); $('#research-detail').innerHTML='<div class="empty">View pins cleared.</div>'; };

// ---- init + polling ------------------------------------------------------ //
loadOverview();
setInterval(() => { const active = document.querySelector('.nav-link.active'); if (active && active.dataset.tab === 'overview') loadOverview(); }, 15000);
