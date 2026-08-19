/* Danus dashboard client. Overview / Fact Graph (echarts DAG) / Global Memory
   (per-channel). Read-only; polls the FastAPI app in app.py. */
'use strict';
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? '' : String(s));
const htmlEsc = (s) => esc(s).replace(/[&<>"']/g, (c) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

let controlToken = '';
if (location.hash.startsWith('#control-token=')) {
  controlToken = decodeURIComponent(location.hash.slice('#control-token='.length));
  history.replaceState(null, '', location.pathname + location.search);
}
async function api(path, options = {}) {
  let r;
  try { r = await fetch(path, options); }
  catch (e) { e.connectionFailure = true; throw e; }
  if (!r.ok) {
    const e = new Error(path + ' ' + r.status);
    e.status = r.status;
    throw e;
  }
  return r.json();
}
function connError(on) { const b = $('#conn-banner'); if (b) b.hidden = !on; }
function connErrorFrom(e) { connError(!!(e && e.connectionFailure)); }

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
  } catch (e) { connErrorFrom(e); }
}

// ---- fact graph (echarts) ------------------------------------------------ //
let graphChart = null;
let currentFactResearchMap = null;
async function loadGraph() {
  try {
    const d = await api('/api/research/map');
    connError(false);
    controlGeneration = d.generation;
    currentFactResearchMap = d;
    renderFactResearchMap(d);
  } catch (e) {
    connErrorFrom(e);
  }
}

function stableTooltip(formatter) {
  return { trigger:'item', confine:true, enterable:false, transitionDuration:0, formatter };
}

function shortLabel(text, limit = 18) {
  text = esc(text).replace(/^v\d+-/, '').replace(/[-_]/g, ' ');
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function readableFactTitle(fact) {
  const raw = esc(fact.title).trim();
  const statement = esc(fact.statement).trim();
  const titleIsStatementPrefix = raw.length >= 60 && statement.startsWith(raw.replace(/…$/, ''));
  if (raw && raw.length <= 120 && !titleIsStatementPrefix) return raw;
  const conclusion = statement.match(/\b(?:Thus|Therefore|Hence|In particular|Then)\b[^.!?]{18,180}[.!?]?/i);
  if (conclusion) return shortLabel(conclusion[0], 96);
  const sentences = statement.split(/(?<=[.!?])\s+/).filter((item) => item.length >= 24);
  const useful = [...sentences].reverse().find((item) => !/\b(?:does not assert|alone|only)\b/i.test(item));
  return shortLabel(useful || raw || statement || `Fact ${fact.fact_id || fact.id}`, 96);
}

function layerFactNodes(facts, edges) {
  const byId = new Map(facts.map((fact) => [fact.fact_id, fact]));
  const predecessors = new Map(facts.map((fact) => [fact.fact_id, []]));
  for (const edge of edges) {
    if (byId.has(edge.source) && byId.has(edge.target)) predecessors.get(edge.target).push(edge.source);
  }
  const closing = facts.filter((fact) => fact.role === 'closing').map((fact) => fact.fact_id);
  const sources = new Set(edges.map((edge) => edge.source));
  const roots = closing.length ? closing : facts.filter((fact) => !sources.has(fact.fact_id)).map((fact) => fact.fact_id);
  const levels = new Map(roots.map((id) => [id, 0]));
  const queue = [...roots];
  while (queue.length) {
    const target = queue.shift();
    const nextLevel = levels.get(target) + 1;
    for (const predecessor of predecessors.get(target) || []) {
      if (!levels.has(predecessor) || nextLevel < levels.get(predecessor)) {
        levels.set(predecessor, nextLevel);
        queue.push(predecessor);
      }
    }
  }
  const lastLevel = Math.max(0, ...levels.values()) + 1;
  facts.forEach((fact) => { if (!levels.has(fact.fact_id)) levels.set(fact.fact_id, lastLevel); });
  const rows = new Map();
  facts.forEach((fact) => {
    const level = levels.get(fact.fact_id);
    if (!rows.has(level)) rows.set(level, []);
    rows.get(level).push(fact);
  });
  const positions = new Map();
  [...rows.entries()].sort((a, b) => a[0] - b[0]).forEach(([level, row]) => {
    row.sort((a, b) => esc(a.title).localeCompare(esc(b.title)));
    row.forEach((fact, index) => positions.set(fact.fact_id, {
      x:(index + 1) * 1000 / (row.length + 1), y:50 + level * 170,
    }));
  });
  return positions;
}

function factSection(title, text, open = false) {
  if (!text) return null;
  const section = el('details', 'fact-section');
  section.open = open;
  section.appendChild(el('summary', 'fact-section-title', title));
  const body = el('div', 'fact-section-body');
  mdmath(body, text);
  section.appendChild(body);
  return section;
}

function renderFactDetail(fact, detail, navigate, allowPin = false) {
  detail.innerHTML = '';
  const card = el('article', 'fact-card' + (viewPins.has(fact.fact_id || fact.id) ? ' pinned' : ''));
  const title = readableFactTitle(fact);
  card.appendChild(el('h2', 'fact-title', title));
  card.appendChild(el('div', 'fid', fact.fact_id || fact.id));
  const meta = el('div', 'fact-meta');
  if (fact.role) meta.appendChild(el('span', 'tag', fact.role));
  if (fact.shared) meta.appendChild(el('span', 'tag', 'shared'));
  if (fact.author) meta.appendChild(el('span', 'tag', fact.author));
  if (fact.problem_id) meta.appendChild(el('span', 'tag', fact.problem_id));
  if (meta.childNodes.length) card.appendChild(meta);
  const statement = factSection('Statement', fact.statement, true);
  const intuition = factSection('Intuition', fact.intuition, false);
  const proof = factSection('Proof', fact.proof, false);
  if (statement) card.appendChild(statement);
  if (intuition) card.appendChild(intuition);
  if (proof) card.appendChild(proof);
  const relations = [
    ['Predecessors', fact.predecessors || []],
    ['Successors', fact.successors || []],
  ];
  for (const [label, ids] of relations) {
    if (!ids.length) continue;
    const section = el('details', 'fact-section');
    section.appendChild(el('summary', 'fact-section-title', `${label} (${ids.length})`));
    const wrap = el('div', 'relation-list');
    ids.forEach((id) => {
      const button = el('button', 'relation-link', id.slice(0, 12));
      button.onclick = () => navigate(id);
      wrap.appendChild(button);
    });
    section.appendChild(wrap);
    card.appendChild(section);
  }
  if (fact.scopes && fact.scopes.length) {
    const scopes = el('div', 'fact-scopes');
    fact.scopes.forEach((scope) => scopes.appendChild(el('span', 'tag', scope.role || scope.scope_type || 'scope')));
    card.appendChild(scopes);
  }
  if (allowPin) {
    const factId = fact.fact_id || fact.id;
    const pin = el('button', 'pin-button', viewPins.has(factId) ? 'Unpin from view' : 'Pin in view');
    pin.onclick = () => { viewPins.has(factId) ? viewPins.delete(factId) : viewPins.add(factId); renderFactDetail(fact, detail, navigate, true); };
    card.appendChild(pin);
  }
  detail.appendChild(card);
}

function renderFactResearchMap(d) {
  const map = researchHierarchy(d);
  if (!graphChart) graphChart = echarts.init($('#graph'));
  graphChart.setOption(researchHierarchyOption(map), true);
  graphChart.off('click');
  graphChart.on('click', async (p) => {
    if (p.data.kind === 'route') await selectFactGraphRoute(p.data.routeId);
    if (p.data.kind === 'obligation') await selectFactGraphObligation(p.data.obligationId);
  });
  $('#graph-map-reset').hidden = false;
  $('#graph-stat').textContent = map.stat;
}

function routeFactSkeleton(group) {
  const facts = group.facts || [];
  const byId = new Map(facts.map((fact) => [fact.fact_id, fact]));
  const visible = facts.filter((fact) => fact.role !== 'support');
  const visibleIds = new Set(visible.map((fact) => fact.fact_id));
  const outgoing = new Map(facts.map((fact) => [fact.fact_id, []]));
  (group.edges || []).forEach((edge) => {
    if (byId.has(edge.source) && byId.has(edge.target)) outgoing.get(edge.source).push(edge.target);
  });
  const links = [];
  const seenLinks = new Set();
  visible.forEach((fact) => {
    const pending = [...outgoing.get(fact.fact_id)];
    const seen = new Set();
    while (pending.length) {
      const target = pending.pop();
      if (seen.has(target)) continue;
      seen.add(target);
      if (visibleIds.has(target)) {
        const key = `${fact.fact_id}:${target}`;
        if (!seenLinks.has(key)) { links.push({source:fact.fact_id,target}); seenLinks.add(key); }
      } else pending.push(...(outgoing.get(target) || []));
    }
  });
  return {facts:visible, links, folded:facts.length - visible.length};
}

function renderRouteFactSkeleton(data, surface = 'fact') {
  const chart = surface === 'fact' ? graphChart : routeChart;
  const detailSelector = surface === 'fact' ? '#fact-detail' : '#research-detail';
  const statSelector = surface === 'fact' ? '#graph-stat' : '#route-stat';
  const skeleton = routeFactSkeleton(data.fact_group);
  const positions = layerFactNodes(skeleton.facts, skeleton.links);
  chart.setOption({
    tooltip:stableTooltip((p) => p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title)}</b><br>${htmlEsc(p.data.role)} · click to expand support` : ''),
    series:[{type:'graph',layout:'none',roam:true,
      data:skeleton.facts.map((fact) => ({id:fact.fact_id,name:fact.fact_id,title:readableFactTitle(fact),role:fact.role,kind:'route-fact',
        ...positions.get(fact.fact_id),symbolSize:fact.role==='closing'?22:fact.role==='input'?15:18,
        itemStyle:{color:fact.role==='closing'?'#16a34a':fact.role==='input'?'#d97706':'#6366f1'}})),links:skeleton.links,
      label:{show:true,position:'bottom',distance:8,formatter:(p)=>shortLabel(p.data.title,24)},
      lineStyle:{color:'#aeb9c9',width:1.4,opacity:.82},edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],
      emphasis:{disabled:true},select:{disabled:true}}]
  }, true);
  chart.off('click');
  chart.on('click', async (p) => {
    if (p.data.kind === 'route-fact') {
      await loadFactNeighborhood(p.data.id, surface, p.data.role);
      await showResearchFact(p.data.id, detailSelector);
    }
  });
  $(statSelector).textContent = `${skeleton.facts.length} route facts · ${skeleton.folded} supporting facts folded`;
  const detail = $(detailSelector); detail.innerHTML = '';
  const card = el('div', 'route-summary-card');
  card.appendChild(el('div', 'eyebrow', 'Route'));
  card.appendChild(el('h2', 'fact-title', data.route.method_title || data.route.id));
  const expected = el('div', 'entry-claim'); mdmath(expected, data.route.expected_result || ''); card.appendChild(expected);
  card.appendChild(el('div', 'muted', `${skeleton.facts.length} route facts · ${skeleton.folded} supporting facts folded · click a fact to expand`));
  detail.appendChild(card);
}

async function loadFactNeighborhood(rootFactId, surface, rootRole) {
  const data = await api(`/api/research/facts/${rootFactId}/neighborhood?direction=predecessors&depth=3&limit=300`);
  const group = {
    facts:(data.nodes || []).map((fact) => ({...fact, role:fact.fact_id === rootFactId ? rootRole : 'support', shared:false})),
    edges:data.edges || [], unexpanded_count:data.truncated ? 1 : 0,
  };
  if (surface === 'fact') renderFactGraphGroup(group);
  else renderFactGraph(group);
}

function renderFactGraphGroup(group) {
  const facts = group.facts || [];
  const ids = new Set(facts.map((fact) => fact.fact_id));
  const links = (group.edges||[]).filter((edge)=>ids.has(edge.source)&&ids.has(edge.target));
  const positions = layerFactNodes(facts, links);
  graphChart.setOption({
    tooltip:stableTooltip((p)=>p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title)}</b><br>${htmlEsc(p.data.role)}${p.data.shared ? ' · shared' : ''}` : ''),
    series:[{type:'graph',layout:'none',roam:true,
      label:{show:true,position:'bottom',distance:7,formatter:(p)=>shortLabel(p.data.title,22)},
      data:facts.map((fact)=>({id:fact.fact_id,name:fact.fact_id,title:readableFactTitle(fact),role:fact.role,shared:fact.shared,kind:'fact',
        ...positions.get(fact.fact_id),symbolSize:fact.role==='closing'?21:fact.role==='direct'?16:12,
        label:{show:fact.role!=='support'},itemStyle:{color:fact.role==='closing'?'#16a34a':fact.role==='support'?'#94a3b8':'#6366f1'}})),
      links,lineStyle:{color:'#b8c2d1',width:1.2,opacity:.72},edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],
      emphasis:{disabled:true},select:{disabled:true}}]
  }, true);
  graphChart.off('click');
  graphChart.on('click', (p) => { if (p.data.kind === 'fact') showResearchFact(p.data.id, '#fact-detail'); });
  $('#graph-stat').textContent = `${facts.length} facts · ${group.unexpanded_count || 0} unexpanded`;
}

async function selectFactGraphRoute(routeId) {
  const d = await api(`/api/research/routes/${routeId}?snapshot=${controlGeneration}`);
  renderRouteFactSkeleton(d, 'fact');
}

async function selectFactGraphObligation(obligationId) {
  const d = await api(`/api/research/obligations/${obligationId}?snapshot=${controlGeneration}`);
  if (d.routes && d.routes.length === 1) await selectFactGraphRoute(d.routes[0].id);
  else renderFactGraphGroup(d.fact_group);
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
  } catch (e) { connErrorFrom(e); memInit = false; }
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
let currentResearchMap = null;

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

function researchHierarchy(d) {
  const nodes = [];
  const links = [];
  const targetId = `target:${d.active_target ? d.active_target.version : 'none'}`;
  nodes.push({ id:targetId, name:d.active_target ? d.active_target.version : 'No active target', kind:'target', symbolSize:26, itemStyle:{color:'#0f766e'} });
  const routesByObligation = new Map(d.obligations.map((obligation) => [obligation.id, []]));
  d.methods.forEach((method) => method.routes.forEach((route) => {
    if (!routesByObligation.has(route.obligation_id)) routesByObligation.set(route.obligation_id, []);
    routesByObligation.get(route.obligation_id).push(route);
  }));
  const routeCount = [...routesByObligation.values()].reduce((count, routes) => count + routes.length, 0);
  let routeIndex = 0;
  routesByObligation.forEach((routes, obligationKey) => {
    const obligationId = `obligation:${obligationKey}`;
    const obligation = d.obligations.find((item) => item.id === obligationKey);
    nodes.push({id:obligationId,name:obligationKey,title:obligation?.statement,kind:'obligation',obligationId:obligationKey,symbolSize:18,itemStyle:{color:'#d97706'}});
    links.push({source:targetId,target:obligationId});
    const children = routes.map((route) => {
      const routeId = `route:${route.id}`;
      const routeNode = {id:routeId,name:route.id,title:route.method_title,kind:'route',routeId:route.id,symbolSize:17,itemStyle:{color:'#2563eb'}};
      nodes.push(routeNode);
      links.push({source:obligationId,target:routeId});
      Object.assign(routeNode, {x:(++routeIndex) * 1000 / (routeCount + 1),y:350});
      return routeNode;
    });
    Object.assign(nodes.find((node) => node.id === obligationId), {
      x:children.length ? children.reduce((sum, child) => sum + child.x, 0) / children.length : 500, y:170,
    });
  });
  Object.assign(nodes[0], {x:500,y:20});
  return { nodes, links, stat:`${d.methods.length} methods · ${d.methods.reduce((n,m)=>n+m.routes.length,0)} routes · ${d.obligations.length} obligations` };
}

function researchHierarchyOption(map) {
  return {
    tooltip:stableTooltip((p)=>p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title || p.data.name)}</b><br>${htmlEsc(p.data.kind)}` : ''),
    series:[{type:'graph',layout:'none',roam:true,
      label:{show:true,position:'bottom',distance:8,formatter:(p)=>p.data.kind === 'target'
        ? p.data.name : shortLabel(p.data.id.split(':').slice(1).join(':'), p.data.kind === 'obligation' ? 14 : 18)},
      data:map.nodes,links:map.links,lineStyle:{color:'#aeb9c9',width:1.4,opacity:.82},
      edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],emphasis:{disabled:true},select:{disabled:true}}]
  };
}

function renderResearchMap(d) {
  const map = researchHierarchy(d);
  if (!routeChart) routeChart = echarts.init($('#route-graph'));
  routeChart.setOption(researchHierarchyOption(map), true);
  routeChart.off('click');
  routeChart.on('click', async (p) => {
    if (p.data.kind === 'route') await selectRoute(p.data.routeId);
    if (p.data.kind === 'obligation') await selectObligation(p.data.obligationId);
  });
  $('#route-stat').textContent = map.stat;
}

function renderFactGraph(group) {
  const facts = group.facts || [];
  const nodeById = Object.fromEntries(facts.map((fact) => [fact.fact_id, fact]));
  const links = (group.edges || []).filter((edge) => nodeById[edge.source] && nodeById[edge.target]);
  const positions = layerFactNodes(facts, links);
  if (!routeChart) routeChart = echarts.init($('#route-graph'));
  routeChart.setOption({
    tooltip:stableTooltip((p) => p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title)}</b><br>${htmlEsc(p.data.role)}${p.data.shared ? ' · shared' : ''}` : ''),
    series: [{ type:'graph', layout:'none', roam:true,
      label:{show:true,position:'bottom',distance:7,formatter:(p)=>shortLabel(p.data.title,22)},
      data:facts.map((fact) => ({ id:fact.fact_id, name:fact.fact_id, title:readableFactTitle(fact), role:fact.role, shared:fact.shared,
        ...positions.get(fact.fact_id), label:{show:fact.role !== 'support'},
        symbolSize:fact.role === 'closing' ? 19 : fact.role === 'direct' ? 15 : 11,
        itemStyle:{color:viewPins.has(fact.fact_id) ? '#ef7f1a' : fact.role === 'closing' ? '#16a34a' : fact.role === 'support' ? '#94a3b8' : '#6366f1'} })),
      links,lineStyle:{color:'#b8c2d1',width:1.2,opacity:.72},edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],
      emphasis:{disabled:true},select:{disabled:true} }]
  }, true);
  routeChart.off('click');
  routeChart.on('click', (p) => { if (p.dataType === 'node') showResearchFact(p.data.id); });
  $('#route-stat').textContent = `${facts.length} nodes · ${group.unexpanded_count || 0} unexpanded`;
}

async function showResearchFact(factId, detailSelector = '#research-detail') {
  const detail = $(detailSelector); detail.innerHTML = '<div class="empty">loading…</div>';
  try {
    const fact = await api(`/api/research/facts/${factId}?include_proof=true`);
    renderFactDetail(fact, detail, (id) => showResearchFact(id, detailSelector), detailSelector === '#research-detail');
  } catch (e) { detail.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function selectRoute(routeId) {
  try {
    const d = await api(`/api/research/routes/${routeId}?snapshot=${controlGeneration}`);
    renderRouteFactSkeleton(d, 'control');
    const detail = $('#research-detail');
    const route = el('div', 'entry'); route.appendChild(el('div', 'entry-author', d.route.method_title));
    route.appendChild(el('div', 'entry-claim', d.route.expected_result || ''));
    route.appendChild(el('div', 'gain-seq', `state ${d.route.state} · gains ${d.checkpoints.map((x)=>x.gain).reverse().join(' → ') || 'none'}`));
    d.obstacles.forEach((x) => route.appendChild(el('div', 'entry-evidence', `Obstacle: ${x.title} (${x.occurrences})`)));
    const recent = d.checkpoints[0] && d.checkpoints[0].report;
    if (recent && recent.unresolved_interfaces) route.appendChild(el('div', 'entry-evidence', `Unresolved: ${recent.unresolved_interfaces.join(', ')}`));
    if (recent && recent.recommended_next_action) route.appendChild(el('div', 'entry-evidence', `Next: ${recent.recommended_next_action}`));
    detail.appendChild(route);
  } catch (e) { connErrorFrom(e); }
}

async function selectObligation(obligationId) {
  try {
    const d = await api(`/api/research/obligations/${obligationId}?snapshot=${controlGeneration}`);
    if (d.routes && d.routes.length === 1) await selectRoute(d.routes[0].id);
    else renderFactGraph(d.fact_group);
  } catch (e) { connErrorFrom(e); }
}

async function loadManifests() {
  const box = $('#context-manifests'); box.innerHTML = '';
  try {
    const d = await api('/api/research/context-manifests?limit=10');
    if (!d.manifests.length) { box.appendChild(el('div','empty','No round context snapshots yet.')); return; }
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
    const obligations=$('#control-obligations'); obligations.innerHTML='';
    d.obligations.forEach((o)=>{
      const row=el('div','entry clickable'); row.appendChild(el('div','entry-author',o.id)); row.appendChild(el('span','tag',o.state));
      if (o.statement.length <= 240) row.appendChild(el('div','entry-claim',o.statement));
      else { const statement=factSection('Full obligation statement',o.statement,false); statement.onclick=(event)=>event.stopPropagation(); row.appendChild(statement); }
      row.onclick=()=>selectObligation(o.id); obligations.appendChild(row);
    });
    await loadManifests();
    currentResearchMap = d;
    renderResearchMap(d);
  } catch (e) { connErrorFrom(e); }
}
$('#clear-pins').onclick = () => { viewPins.clear(); $('#research-detail').innerHTML='<div class="empty">View pins cleared.</div>'; };
$('#show-research-map').onclick = () => { if (currentResearchMap) renderResearchMap(currentResearchMap); };
$('#graph-map-reset').onclick = () => { if (currentFactResearchMap) renderFactResearchMap(currentFactResearchMap); };

// ---- init + polling ------------------------------------------------------ //
loadOverview();
setInterval(() => { const active = document.querySelector('.nav-link.active'); if (active && active.dataset.tab === 'overview') loadOverview(); }, 15000);
