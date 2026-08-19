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
let currentArchiveGraph = null;
let graphMode = 'exploration';
let graphTrail = [];
let graphRefreshBusy = false;
async function loadGraph() {
  if (graphRefreshBusy) return;
  graphRefreshBusy = true;
  try {
    const d = await api('/api/research/map');
    connError(false);
    const changed = !currentFactResearchMap || currentFactResearchMap.generation !== d.generation;
    controlGeneration = d.generation;
    currentFactResearchMap = d;
    currentArchiveGraph = d.active_target ? null : await api('/api/research/archive');
    $('.graph-toggle').hidden = !d.active_target;
    $('#graph-description').textContent = d.active_target
      ? 'Target conclusions contain exploration attempts; verified facts may be reused across conclusions.'
      : 'Migrated V1 archive: headline proof and all facts are derived from TARGET.md plus the fact DAG; no cases or routes are inferred.';
    if (!graphTrail.length) resetGraphNavigation(d);
    else if (changed) {
      graphTrail[graphTrail.length - 1].roam = graphRoam();
      await refreshSelectedGraphView(d);
      renderGraphView();
    }
  } catch (e) {
    connErrorFrom(e);
  } finally { graphRefreshBusy = false; }
}

async function refreshSelectedGraphView(d) {
  const view = graphTrail[graphTrail.length - 1];
  const attemptId = view.kind === 'attempt' ? view.data.route.id : view.attemptId;
  if (!attemptId) return;
  const data = await api(`/api/research/routes/${attemptId}?snapshot=${d.generation}`);
  if (view.kind === 'attempt') { view.data = data; return; }
  const proposition = data.fact_group.proof_structure?.proposition_groups?.find((group)=>group.id===view.propositionGroupId);
  if (!proposition) return;
  view.group = propositionFactGroup(data.fact_group, proposition);
  view.rootFactId = proposition.root_fact_id;
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
  const summary = esc(fact.summary).trim();
  const statement = esc(fact.statement).trim();
  const titleIsStatementPrefix = raw.length >= 60 && statement.startsWith(raw.replace(/…$/, ''));
  if (raw && raw.length <= 120 && !titleIsStatementPrefix) return raw;
  if (summary) return shortLabel(summary, 96);
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

function layerProofGroups(groups, edges) {
  const ids = new Set(groups.map((group)=>group.id));
  const children = new Map(groups.map((group)=>[group.id,[]]));
  const indegree = new Map(groups.map((group)=>[group.id,0]));
  edges.forEach((edge)=>{ if(ids.has(edge.source)&&ids.has(edge.target)){ children.get(edge.source).push(edge.target); indegree.set(edge.target,indegree.get(edge.target)+1); } });
  const level = new Map(groups.filter((group)=>indegree.get(group.id)===0).map((group)=>[group.id,0]));
  const queue = [...level.keys()].sort();
  while(queue.length){ const source=queue.shift(); for(const target of children.get(source)){
    level.set(target,Math.max(level.get(target)||0,level.get(source)+1)); indegree.set(target,indegree.get(target)-1); if(indegree.get(target)===0) queue.push(target);
  }}
  const unresolved=Math.max(0,...level.values())+1;
  groups.forEach((group)=>{ if(!level.has(group.id)) level.set(group.id,unresolved); });
  const maxLevel=Math.max(1,...level.values());
  const columns=new Map();
  groups.forEach((group)=>{ const n=level.get(group.id); if(!columns.has(n)) columns.set(n,[]); columns.get(n).push(group); });
  const positions=new Map();
  columns.forEach((column,n)=>{ column.sort((a,b)=>a.title.localeCompare(b.title)); column.forEach((group,index)=>positions.set(group.id,{
    x:100+n*800/maxLevel,y:(index+1)*560/(column.length+1),
  })); });
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
  if (Array.isArray(fact.tags)) fact.tags.forEach((tag) => meta.appendChild(el('span', 'tag', tag)));
  if (meta.childNodes.length) card.appendChild(meta);
  if (fact.summary) card.appendChild(el('div', 'entry-claim', fact.summary));
  if (fact.method) card.appendChild(el('div', 'muted', `Method: ${fact.method}`));
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

function orderedConclusions(d) {
  const order = new Map((d.active_target?.required_conclusions || []).map((item, index) => [item.id, index]));
  return [...(d.exploration?.conclusions || [])].sort((left, right) =>
    (order.get(shortLabel(left.id, 99)) ?? 999) - (order.get(shortLabel(right.id, 99)) ?? 999) || left.id.localeCompare(right.id));
}

function graphRoam() {
  const series = graphChart?.getOption()?.series?.[0];
  return series ? {zoom:series.zoom, center:series.center} : null;
}

function setGraph(option, onClick, stat, legend) {
  if (!graphChart) graphChart = echarts.init($('#graph'));
  graphChart.setOption(option, true);
  graphChart.off('click');
  if (onClick) graphChart.on('click', onClick);
  $('#graph-stat').textContent = stat;
  $('#graph-legend').innerHTML = legend;
}

function resetGraphNavigation(d = currentFactResearchMap) {
  graphTrail = [{kind:'overview', label:d?.active_target ? 'Target' : 'Fact archive', data:d, roam:null}];
  renderGraphView();
}

function pushGraphView(view) {
  if (graphTrail.length) graphTrail[graphTrail.length - 1].roam = graphRoam();
  graphTrail.push({...view, roam:null});
  renderGraphView();
}

function goGraphBreadcrumb(index) {
  if (index >= graphTrail.length - 1) return;
  graphTrail[graphTrail.length - 1].roam = graphRoam();
  graphTrail = graphTrail.slice(0, index + 1);
  renderGraphView();
}

function renderGraphBreadcrumb() {
  const breadcrumb = $('#graph-breadcrumb'); breadcrumb.innerHTML = '';
  graphTrail.forEach((view, index) => {
    if (index) breadcrumb.appendChild(el('span', 'separator', '›'));
    const button = el('button', '', shortLabel(view.label, 34));
    button.type = 'button';
    button.disabled = index === graphTrail.length - 1;
    button.onclick = () => goGraphBreadcrumb(index);
    breadcrumb.appendChild(button);
  });
}

function renderGraphView() {
  if (!currentFactResearchMap || !graphTrail.length) return;
  renderGraphBreadcrumb();
  const view = graphTrail[graphTrail.length - 1];
  if (view.kind === 'overview') {
    if (currentFactResearchMap.active_target) renderTargetOverview(currentFactResearchMap);
    else renderArchiveOverview(currentFactResearchMap, currentArchiveGraph);
  }
  if (view.kind === 'archive-proof' || view.kind === 'archive-all') renderArchiveDag(currentArchiveGraph, view.kind === 'archive-proof');
  if (view.kind === 'shared' || view.kind === 'shared-fact') renderSharedFacts(currentFactResearchMap, view.factId);
  if (view.kind === 'conclusion') renderConclusionAttempts(currentFactResearchMap, view.conclusionId);
  if (view.kind === 'attempt') renderAttemptProofStructure(view.data, 'fact');
  if (view.kind === 'group' || view.kind === 'fact') {
    renderFactGraphGroup(view.group, view);
    showResearchFact(view.kind === 'fact' ? view.factId : view.rootFactId, '#fact-detail');
  }
  if (view.roam && graphChart) graphChart.setOption({series:[view.roam]}, false);
}

function renderArchiveOverview(d, archive) {
  if (!archive) return;
  const byId = new Map(archive.nodes.map((fact) => [fact.fact_id, fact]));
  const headlineIds = new Set(archive.headline_fact_ids || []);
  const directEdges = archive.edges.filter((edge) => headlineIds.has(edge.target));
  const directIds = [...new Set(directEdges.map((edge) => edge.source))];
  const nodes = [{
    id:'archive-root', name:archive.project, kind:'archive-root', x:500, y:40,
    symbol:'roundRect', symbolSize:[220,46], label:{show:true,position:'inside',fontWeight:700,color:'#0f766e'},
    itemStyle:{color:'#ecfdf5',borderColor:'#5eead4',borderWidth:1.5},
  }, {
    id:'archive-all', name:`All ${archive.fact_count} facts`, kind:'archive-all', x:165, y:245,
    symbol:'roundRect', symbolSize:[170,52], label:{show:true,position:'inside',fontWeight:700,color:'#334155'},
    itemStyle:{color:'#f8fafc',borderColor:'#94a3b8',borderWidth:1.5},
  }];
  const links = [{source:'archive-root',target:'archive-all',kind:'contains',lineStyle:{color:'#cbd5e1',width:1.2,opacity:.75}}];
  (archive.headline_fact_ids || []).forEach((factId, index) => {
    const fact = byId.get(factId) || {title:factId};
    const y = 190 + index * 115;
    nodes.push({id:factId,name:readableFactTitle(fact),title:fact.title,kind:'headline',factId,x:820,y,
      symbol:'roundRect',symbolSize:[210,62],label:{show:true,position:'inside',formatter:(p)=>shortLabel(p.data.name,28),fontWeight:700,color:'#047857'},
      itemStyle:{color:'#ecfdf5',borderColor:'#34d399',borderWidth:2}});
    links.push({source:'archive-root',target:factId,kind:'contains',lineStyle:{color:'#cbd5e1',width:1.2,opacity:.75}});
  });
  directIds.forEach((factId, index) => {
    const fact = byId.get(factId) || {title:factId};
    nodes.push({id:`premise:${factId}`,name:readableFactTitle(fact),title:fact.title,kind:'direct-premise',factId,
      x:455,y:125+(index+1)*360/(directIds.length+1),symbol:'roundRect',symbolSize:[175,44],
      label:{show:true,position:'inside',formatter:(p)=>shortLabel(p.data.name,23),color:'#3730a3'},
      itemStyle:{color:'#eef2ff',borderColor:'#a5b4fc',borderWidth:1.3}});
  });
  directEdges.forEach((edge) => links.push({source:`premise:${edge.source}`,target:edge.target,kind:'fact',
    lineStyle:{color:'#64748b',width:1.3,opacity:.6}}));
  setGraph({
    tooltip:stableTooltip((p) => p.dataType === 'edge'
      ? (p.data.kind === 'fact' ? 'verified predecessor → derived fact' : 'archive containment')
      : `<b>${htmlEsc(p.data.title || p.data.name)}</b><br>${p.data.kind === 'direct-premise' ? 'direct premise · click for complete headline proof' : p.data.kind === 'headline' ? 'TARGET.md headline fact · click for complete proof' : 'click to inspect'}`),
    series:[{type:'graph',layout:'none',roam:true,data:nodes,links,label:{show:true},lineStyle:{opacity:.7},
      edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],emphasis:{focus:'adjacency'},select:{disabled:true}}],
  }, (p) => {
    if (p.data.kind === 'archive-all') pushGraphView({kind:'archive-all',label:'All archived facts'});
    if (p.data.kind === 'headline' || p.data.kind === 'direct-premise') {
      pushGraphView({kind:'archive-proof',label:'Headline proof'});
      showResearchFact(p.data.factId, '#fact-detail');
    }
  }, `${archive.fact_count} indexed facts · ${archive.proof_fact_count} in headline proof · ${archive.direct_premise_count} direct premises`,
  '<i class="succeeded"></i>TARGET.md headline <i class="active"></i>direct premise <i class="superseded"></i>other archived fact');
  $('#fact-detail').innerHTML = `<div class="route-summary-card"><div class="eyebrow">Migrated V1 fact archive</div><h2 class="fact-title">${htmlEsc(archive.project)}</h2><div class="muted">${archive.proof_fact_count} facts support the recorded headline theorem; ${archive.outside_proof_count} other facts record parallel exploration or unused branches. Direct premises are DAG edges, not case splits.</div></div>`;
}

function renderArchiveDag(archive, proofOnly) {
  if (!archive) return;
  const facts = archive.nodes.filter((fact) => !proofOnly || fact.role !== 'unassigned');
  const ids = new Set(facts.map((fact) => fact.fact_id));
  const links = archive.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const positions = layerFactNodes(facts, links);
  const nodes = facts.map((fact) => ({
    id:fact.fact_id,name:fact.fact_id,title:readableFactTitle(fact),role:fact.role,terminal:fact.terminal,
    ...positions.get(fact.fact_id),symbol:'circle',
    symbolSize:fact.role === 'closing' ? 20 : fact.role === 'direct' ? 13 : proofOnly ? 7 : 4,
    label:{show:fact.role === 'closing' || fact.role === 'direct',position:'bottom',distance:6,formatter:(p)=>shortLabel(p.data.title,24)},
    itemStyle:{color:fact.role === 'closing' ? '#16a34a' : fact.role === 'direct' ? '#4f46e5' : fact.role === 'support' ? '#60a5fa' : '#94a3b8',
      borderColor:fact.role === 'closing' ? '#064e3b' : '#fff',borderWidth:fact.role === 'closing' ? 2.5 : .8},
  }));
  setGraph({
    tooltip:stableTooltip((p)=>p.dataType === 'node' ? `<b>${htmlEsc(p.data.title)}</b><br>${htmlEsc(p.data.role)} · ${p.data.id}` : 'verified predecessor → derived fact'),
    series:[{type:'graph',layout:'none',roam:true,data:nodes,links,label:{show:false},
      lineStyle:{color:proofOnly?'#94a3b8':'#cbd5e1',width:proofOnly?1:.55,opacity:proofOnly?.45:.2},
      edgeSymbol:['none','arrow'],edgeSymbolSize:[0,proofOnly?4:2],emphasis:{focus:'adjacency',label:{show:true,formatter:(p)=>shortLabel(p.data.title,30)}},select:{disabled:true}}],
  }, (p)=>{ if(p.dataType === 'node') showResearchFact(p.data.id, '#fact-detail'); },
  `${facts.length} facts · ${links.length} verified dependency edges${proofOnly ? ' · complete TARGET.md closure' : ' · complete archive'}`,
  '<i class="succeeded"></i>headline <i class="active"></i>direct premise <i style="background:#60a5fa"></i>headline support <i class="superseded"></i>outside headline proof');
  $('#fact-detail').innerHTML = `<div class="route-summary-card"><div class="eyebrow">${proofOnly ? 'Headline proof' : 'Complete fact archive'}</div><h2 class="fact-title">${proofOnly ? 'All facts needed by TARGET.md' : 'Every indexed V1 fact'}</h2><div class="muted">Zoom and pan the complete graph; labels appear for headline/direct facts and on hover. Click a node for its statement and proof.</div></div>`;
}

function renderTargetOverview(d) {
  const conclusions = orderedConclusions(d);
  const shared = d.exploration?.shared_facts || [];
  const targetId = 'target-overview';
  const nodes = [{
    id:targetId, name:'Research target', title:d.active_target?.statement || '', kind:'target-overview', x:500, y:45,
    symbol:'roundRect', symbolSize:[190,46], label:{show:true,position:'inside',fontWeight:700,color:'#0f766e'},
    itemStyle:{color:'#ecfdf5',borderColor:'#5eead4',borderWidth:1.5},
  }];
  const links = [];
  if (shared.length) {
    nodes.push({id:'shared-pool',name:`${shared.length} cross-conclusion facts`,kind:'shared-pool',x:500,y:150,
      symbol:'roundRect',symbolSize:[210,38],label:{show:true,position:'inside',fontWeight:650,color:'#4338ca'},
      itemStyle:{color:'#eef2ff',borderColor:'#a5b4fc',borderWidth:1.5}});
  }
  conclusions.forEach((conclusion, index) => {
    const x = (index + 1) * 1000 / (conclusions.length + 1);
    const code = shortLabel(conclusion.id, 99);
    nodes.push({id:`conclusion:${conclusion.id}`,name:code,title:conclusion.title,state:conclusion.state,kind:'conclusion',conclusionId:conclusion.id,
      attemptCount:conclusion.attempt_ids.length,x,y:290,symbol:'roundRect',symbolSize:[150,58],
      label:{show:true,position:'inside',fontWeight:700,color:conclusion.state==='closed'?'#047857':'#9a3412'},
      itemStyle:{color:conclusion.state==='closed'?'#ecfdf5':'#fff7ed',borderColor:conclusion.state==='closed'?'#6ee7b7':'#fdba74',borderWidth:1.5}});
    links.push({source:targetId,target:`conclusion:${conclusion.id}`,kind:'contains',lineStyle:{color:'#cbd5e1',width:1.2,opacity:.75}});
    if (shared.some((fact) => fact.obligations.some((item) => item.id === conclusion.id))) {
      links.push({source:'shared-pool',target:`conclusion:${conclusion.id}`,kind:'reuse',lineStyle:{color:'#818cf8',type:'dotted',width:1.2,opacity:.55}});
    }
  });
  setGraph({
    tooltip:stableTooltip((p) => {
      if (p.dataType === 'edge') return p.data.kind === 'reuse' ? 'verified facts reused in this conclusion' : 'required conclusion';
      if (p.data.kind === 'conclusion') return `<b>${htmlEsc(p.data.name)}</b><br>${htmlEsc(p.data.title)}<br>${p.data.attemptCount} exploration attempts`;
      if (p.data.kind === 'shared-pool') return `${shared.length} verified facts observed in two or more conclusions · click to inspect`;
      return `<b>Research target</b><br>${htmlEsc(p.data.title)}<br>${htmlEsc(d.active_target?.version || '')}`;
    }),
    series:[{type:'graph',layout:'none',roam:true,data:nodes,links,label:{show:true},lineStyle:{opacity:.7},edgeSymbol:['none','none'],emphasis:{focus:'adjacency'},select:{disabled:true}}],
  }, (p) => {
    if (p.data.kind === 'conclusion') pushGraphView({kind:'conclusion',label:shortLabel(p.data.conclusionId,99),conclusionId:p.data.conclusionId});
    if (p.data.kind === 'shared-pool') pushGraphView({kind:'shared',label:'Shared facts'});
  }, `${conclusions.length} required conclusions · ${shared.length} cross-conclusion facts`,
  '<b class="fact-edge"></b>containment <b class="reuse-edge"></b>cross-conclusion reuse');
  $('#fact-detail').innerHTML = '<div class="route-summary-card"><div class="eyebrow">Target overview</div><h2 class="fact-title">Required conclusions, not proof routes</h2><div class="muted">Choose a conclusion to inspect its exploration attempts, or open the shared fact pool.</div></div>';
}

function renderSharedFacts(d, selectedFactId = null) {
  const facts = d.exploration?.shared_facts || [];
  const conclusions = orderedConclusions(d);
  const nodes = [];
  const links = [];
  facts.forEach((fact, index) => {
    const column = index % 7, row = Math.floor(index / 7);
    nodes.push({id:`shared:${fact.id}`,name:fact.id,title:fact.title,kind:'shared-fact',factId:fact.id,
      x:90 + column * 135,y:70 + row * 100,symbol:'diamond',symbolSize:selectedFactId===fact.id?22:15,
      itemStyle:{color:selectedFactId===fact.id?'#4338ca':'#818cf8',borderColor:'#fff',borderWidth:1.5},label:{show:false}});
    fact.obligations.forEach((scope) => {
      const produced = scope.roles.some((role) => role === 'direct' || role === 'closing');
      links.push({source:produced?`conclusion:${scope.id}`:`shared:${fact.id}`,target:produced?`shared:${fact.id}`:`conclusion:${scope.id}`,
        kind:produced?'produced':'consumed',lineStyle:{color:produced?'#16a34a':'#64748b',width:1.1,opacity:.34,curveness:produced?-.05:.05}});
    });
  });
  conclusions.forEach((conclusion, index) => nodes.push({id:`conclusion:${conclusion.id}`,name:shortLabel(conclusion.id,99),title:conclusion.title,
    kind:'conclusion',conclusionId:conclusion.id,x:(index+1)*1000/(conclusions.length+1),y:460,symbol:'roundRect',symbolSize:[125,42],
    itemStyle:{color:'#f8fafc',borderColor:'#94a3b8',borderWidth:1.3},label:{show:true,position:'inside',fontWeight:700,color:'#334155'}}));
  setGraph({
    tooltip:stableTooltip((p) => {
      if (p.dataType === 'edge') return p.data.kind === 'produced' ? 'verified fact produced here' : 'verified fact used as input here';
      return p.data.kind === 'shared-fact' ? `<b>${htmlEsc(p.data.title)}</b><br>${p.data.factId} · click to inspect` : `<b>${htmlEsc(p.data.name)}</b><br>${htmlEsc(p.data.title)}`;
    }),
    series:[{type:'graph',layout:'none',roam:true,data:nodes,links,label:{show:false},lineStyle:{opacity:.35},edgeSymbol:['none','arrow'],edgeSymbolSize:[0,5],
      emphasis:{focus:'adjacency',label:{show:true,formatter:(p)=>p.data.kind==='shared-fact'?shortLabel(p.data.title,30):p.data.name}},select:{disabled:true}}],
  }, (p) => {
    if (p.data.kind === 'shared-fact') {
      if (graphTrail[graphTrail.length - 1].kind === 'shared-fact') graphTrail.pop();
      pushGraphView({kind:'shared-fact',label:`Fact · ${p.data.title}`,factId:p.data.factId});
    }
    if (p.data.kind === 'conclusion') pushGraphView({kind:'conclusion',label:shortLabel(p.data.conclusionId,99),conclusionId:p.data.conclusionId});
  }, `${facts.length} facts reused across conclusions`,
  '<i class="active"></i>shared fact <b class="fact-edge"></b>produced / used');
  if (selectedFactId) showResearchFact(selectedFactId, '#fact-detail');
  else $('#fact-detail').innerHTML = '<div class="route-summary-card"><div class="eyebrow">Verified-fact reuse</div><h2 class="fact-title">Cross-conclusion facts</h2><div class="muted">Direction shows where a verified fact was produced and where it was registered as input. Reuse is derived from scopes; it does not assert mathematical independence.</div></div>';
}

function renderAttemptProofStructure(data, surface = 'fact') {
  const chart = surface === 'fact' ? graphChart : routeChart;
  const detailSelector = surface === 'fact' ? '#fact-detail' : '#research-detail';
  const statSelector = surface === 'fact' ? '#graph-stat' : '#route-stat';
  if (surface === 'fact') $('#graph-legend').innerHTML = 'hollow input · filled derived · thick border closing';
  const structure = data.fact_group.proof_structure || {components:[],proposition_groups:[],edges:[],fact_count:0,unexpanded_count:0};
  const groups = structure.proposition_groups || [];
  const groupById = new Map(groups.map((group) => [group.id, group]));
  const positions = layerProofGroups(groups, structure.edges || []);
  const groupNodes = groups.map((group) => ({id:group.id,name:group.title,title:group.title,kind:'proposition-group',
    groupId:group.id,factCount:group.fact_ids.length,role:group.role,...positions.get(group.id),
    symbol:group.role==='closing'?'diamond':'circle',symbolSize:group.role==='closing'?22:group.role==='input'?17:15,
    itemStyle:{color:group.role==='input'?'#fff':group.role==='closing'?'#10b981':'#6366f1',
      borderColor:group.role==='input'?'#6366f1':'#fff',borderWidth:group.role==='closing'?3:1.5}}));
  const links = [...(structure.edges || [])];
  chart.setOption({
    tooltip:stableTooltip((p) => p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title)}</b><br>proposition group · ${p.data.factCount} facts · click to inspect`
      : 'verified-fact dependency'),
    series:[{type:'graph',layout:'none',roam:true,
      data:groupNodes,links,
      label:{show:false,position:'bottom',distance:7,color:'#334155',fontSize:11,formatter:(p)=>shortLabel(p.data.name,28)},
      lineStyle:{color:'#94a3b8',width:1.2,opacity:.25,curveness:.04},edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],
      emphasis:{focus:'adjacency',label:{show:true}},select:{disabled:true}}]
  }, true);
  chart.off('click');
  chart.on('click', async (p) => {
    if (p.data.kind === 'proposition-group') {
      const group = groupById.get(p.data.groupId);
      if (group && surface === 'fact') pushGraphView({kind:'group',label:`Proposition · ${group.title}`,
        group:propositionFactGroup(data.fact_group,group),rootFactId:group.root_fact_id,
        attemptId:data.route.id,propositionGroupId:group.id});
      if (group && surface !== 'fact') await openPropositionGroup(data.fact_group, group, surface);
    }
  });
  $(statSelector).textContent = `${groups.length} proposition groups · ${structure.fact_count} facts`;
  const detail = $(detailSelector); detail.innerHTML = '';
  const card = el('div', 'route-summary-card');
  card.appendChild(el('div', 'eyebrow', 'Exploration attempt'));
  card.appendChild(el('h2', 'fact-title', data.route.method_title || data.route.id));
  const expected = el('div', 'entry-claim'); mdmath(expected, data.route.expected_result || ''); card.appendChild(expected);
  card.appendChild(el('div', 'muted', `${data.route.state} · ${groups.length} proposition groups · ${structure.fact_count} facts${structure.unexpanded_count ? ` · ${structure.unexpanded_count} unexpanded` : ''}`));
  if (data.route.fallback_route_ids?.length) card.appendChild(el('div', 'muted', `Pivot candidates: ${data.route.fallback_route_ids.join(', ')}`));
  detail.appendChild(card);
}

function propositionFactGroup(factGroup, propositionGroup) {
  const ids = new Set(propositionGroup.fact_ids || []);
  return {
    facts:(factGroup.facts || []).filter((fact) => ids.has(fact.fact_id)).map((fact) => ({
      ...fact, role:fact.fact_id === propositionGroup.root_fact_id ? propositionGroup.role : 'support',
      shared:(propositionGroup.shared_fact_ids || []).includes(fact.fact_id),
    })),
    edges:(factGroup.edges || []).filter((edge) => ids.has(edge.source) && ids.has(edge.target)),
    unexpanded_count:factGroup.proof_structure?.unexpanded_count || 0,
  };
}

async function openPropositionGroup(factGroup, propositionGroup, surface) {
  const group = propositionFactGroup(factGroup, propositionGroup);
  if (surface === 'fact') renderFactGraphGroup(group);
  else renderFactGraph(group);
  await showResearchFact(propositionGroup.root_fact_id, surface === 'fact' ? '#fact-detail' : '#research-detail');
}

function renderFactGraphGroup(group, navigation = null) {
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
  graphChart.on('click', (p) => {
    if (p.data.kind !== 'fact') return;
    if (graphTrail[graphTrail.length - 1]?.kind === 'fact') graphTrail.pop();
    pushGraphView({kind:'fact',label:`Fact · ${p.data.title}`,group,factId:p.data.id,rootFactId:navigation?.rootFactId || p.data.id,
      attemptId:navigation?.attemptId,propositionGroupId:navigation?.propositionGroupId});
  });
  $('#graph-legend').innerHTML = '<i class="active"></i>proposition fact <i class="superseded"></i>support fact';
  $('#graph-stat').textContent = `${facts.length} facts · ${group.unexpanded_count || 0} unexpanded`;
}

async function selectFactGraphAttempt(attemptId) {
  const d = await api(`/api/research/routes/${attemptId}?snapshot=${controlGeneration}`);
  pushGraphView({kind:'attempt',label:d.route.method_title || d.route.id,data:d});
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

const ATTEMPT_STATE_COLORS = {
  active:'#2563eb', succeeded:'#16a34a', stalled:'#d97706',
  refuted:'#dc2626', superseded:'#64748b', proposed:'#7c3aed', failed:'#dc2626',
};

function conclusionAttemptMap(d, conclusionId) {
  const exploration = d.exploration || {attempts:[],edges:[],cycle_groups:[]};
  const attempts = exploration.attempts.filter((attempt) => attempt.obligation_id === conclusionId);
  const ids = new Set(attempts.map((attempt) => attempt.id));
  const edgeType = graphMode === 'facts' ? 'fact_flow' : 'fallback';
  const edges = exploration.edges.filter((edge) => edge.type === edgeType && ids.has(edge.source) && ids.has(edge.target));
  const cycles = graphMode === 'exploration' ? exploration.cycle_groups.filter((group) => group.obligation_id === conclusionId) : [];
  const owner = new Map(attempts.map((attempt) => [attempt.id, attempt.id]));
  cycles.forEach((group) => group.attempt_ids.forEach((id) => owner.set(id, group.id)));
  const componentById = new Map();
  attempts.forEach((attempt) => {
    const id = owner.get(attempt.id);
    if (!componentById.has(id)) componentById.set(id, {id, members:[], cycle:id.startsWith('cycle:')});
    componentById.get(id).members.push(attempt);
  });
  const componentEdges = new Map();
  edges.forEach((edge) => {
    const source = owner.get(edge.source), target = owner.get(edge.target);
    if (source === target) return;
    const key = `${source}\u0000${target}`;
    const row = componentEdges.get(key) || {source,target,kind:edgeType,factCount:0};
    row.factCount += edge.fact_count || 0;
    componentEdges.set(key,row);
  });
  const components = [...componentById.values()];
  const children = new Map(components.map((item) => [item.id, []]));
  const indegree = new Map(components.map((item) => [item.id, 0]));
  componentEdges.forEach((edge) => { children.get(edge.source).push(edge.target); indegree.set(edge.target,indegree.get(edge.target)+1); });
  const level = new Map(components.filter((item) => indegree.get(item.id)===0).map((item)=>[item.id,0]));
  const queue = [...level.keys()].sort();
  while (queue.length) {
    const source = queue.shift();
    for (const target of children.get(source)) {
      level.set(target,Math.max(level.get(target)||0,level.get(source)+1));
      indegree.set(target,indegree.get(target)-1);
      if (indegree.get(target)===0) queue.push(target);
    }
  }
  const unresolved = Math.max(0,...level.values())+1;
  components.forEach((item)=>{ if(!level.has(item.id)) level.set(item.id,unresolved); });
  const columns = new Map();
  components.forEach((item)=>{ const n=level.get(item.id); if(!columns.has(n)) columns.set(n,[]); columns.get(n).push(item); });
  const maxLevel = Math.max(1,...level.values());
  const nodes = [];
  columns.forEach((column, levelIndex) => {
    column.sort((left,right)=>left.id.localeCompare(right.id));
    column.forEach((component,index) => {
      const terminal = children.get(component.id).length===0;
      const centerY=60+(index%10+1)*500/(Math.min(column.length,10)+1);
      component.members.forEach((attempt,memberIndex)=>nodes.push({id:attempt.id,name:attempt.id,title:attempt.title,
        kind:'attempt',attemptId:attempt.id,state:attempt.state,factCount:attempt.fact_count,terminal,
        x:90+levelIndex*820/maxLevel+Math.floor(index/10)*22,
        y:centerY+(memberIndex-(component.members.length-1)/2)*34,
        symbol:'circle',symbolSize:attempt.state==='active'||attempt.state==='refuted'?18:14,label:{show:false},
        itemStyle:{color:ATTEMPT_STATE_COLORS[attempt.state]||'#7c3aed',borderColor:terminal?'#0f172a':'#fff',borderWidth:terminal?3:1.5}}));
    });
  });
  const links = edges.map((edge)=>({source:edge.source,target:edge.target,kind:edge.type,factCount:edge.fact_count||0,
    lineStyle:edge.type==='fallback'?{color:'#d97706',type:'dashed',width:1.3,opacity:.48,curveness:.04}:
      {color:'#64748b',type:'solid',width:Math.min(3,1+(edge.fact_count||0)/4),opacity:.32,curveness:-.04}}));
  return {attempts,nodes,links,cycles,edgeCount:edges.length};
}

function renderConclusionAttempts(d, conclusionId) {
  const conclusion = (d.exploration?.conclusions || []).find((item)=>item.id===conclusionId);
  if (!conclusion) return;
  const map = conclusionAttemptMap(d, conclusionId);
  setGraph({
    tooltip:stableTooltip((p)=>{
      if(p.dataType==='edge') return p.data.kind==='fallback'?'fallback / pivot between attempts':`${p.data.factCount} verified facts passed as input`;
      return `<b>${htmlEsc(p.data.title)}</b><br>${htmlEsc(p.data.state)} · ${p.data.factCount} scoped facts${p.data.terminal?' · terminal graph component':''} · click to inspect`;
    }),
    series:[{type:'graph',layout:'none',roam:true,data:map.nodes,links:map.links,label:{show:false,position:'bottom',distance:7},lineStyle:{opacity:.4},
      edgeSymbol:['none','arrow'],edgeSymbolSize:[0,6],emphasis:{focus:'adjacency',label:{show:true,formatter:(p)=>shortLabel(p.data.name,28)}},select:{disabled:true}}],
  }, async (p)=>{
    if(p.data.kind==='attempt') await selectFactGraphAttempt(p.data.attemptId);
  }, `${map.attempts.length} attempts · ${map.edgeCount} ${graphMode==='facts'?'fact-flow links':'pivot links'}`,
  '<i class="active"></i>active <i class="succeeded"></i>succeeded <i class="stalled"></i>stalled <i class="refuted"></i>refuted <i class="superseded"></i>superseded <b class="fallback-edge"></b>'+(graphMode==='facts'?'fact flow':'fallback/pivot')+' · outlined terminal component');
  const detail=$('#fact-detail'); detail.innerHTML='';
  const card=el('div','route-summary-card'); card.appendChild(el('div','eyebrow','Required conclusion'));
  card.appendChild(el('h2','fact-title',shortLabel(conclusion.id,99)));
  const statement=el('div','entry-claim'); mdmath(statement,conclusion.title); card.appendChild(statement);
  card.appendChild(el('div','muted',`${conclusion.state} · ${map.attempts.length} exploration attempts · rightmost means graph depth, not theorem completion`));
  detail.appendChild(card);
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
      const routeNode = {id:routeId,name:route.id,title:route.method_title,kind:'work-item',routeId:route.id,symbolSize:17,itemStyle:{color:'#2563eb'}};
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
  return { nodes, links, stat:`${d.methods.reduce((n,m)=>n+m.routes.length,0)} work items · ${d.obligations.length} obligations` };
}

function researchHierarchyOption(map) {
  return {
    tooltip:stableTooltip((p)=>p.dataType === 'node'
      ? `<b>${htmlEsc(p.data.title || p.data.name)}</b><br>${htmlEsc(p.data.kind)}` : ''),
    series:[{type:'graph',layout:'none',roam:true,
      label:{show:true,position:'bottom',distance:8,formatter:(p)=>p.data.kind === 'target'
        ? p.data.name : shortLabel(p.data.name, p.data.kind === 'obligation' || p.data.kind === 'proof-framework' ? 14 : 22)},
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
    if (p.data.kind === 'work-item') await selectRoute(p.data.routeId);
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
    renderAttemptProofStructure(d, 'control');
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
      ['Work items', d.methods.reduce((n,m)=>n+m.routes.length,0), `${d.methods.length} method labels`],
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
$('#graph-mode').onchange = (event) => {
  graphMode = event.target.value;
  if (!currentFactResearchMap) return;
  resetGraphNavigation(currentFactResearchMap);
  if (graphMode === 'facts' && currentFactResearchMap.exploration?.shared_facts?.length) {
    pushGraphView({kind:'shared',label:'Shared facts'});
  }
};

// ---- init + polling ------------------------------------------------------ //
loadOverview();
setInterval(() => {
  const active = document.querySelector('.nav-link.active');
  if (active?.dataset.tab === 'overview') loadOverview();
  if (active?.dataset.tab === 'graph') loadGraph();
}, 15000);
