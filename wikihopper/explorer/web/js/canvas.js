const nodes = new vis.DataSet([]);
const edges = new vis.DataSet([]);
const container = document.getElementById("net");
const network = new vis.Network(container, {nodes, edges}, {
  physics: { stabilization: false, barnesHut: { gravitationalConstant:-8000,
             springLength:130, springConstant:0.02, damping:0.4 } },
  interaction: { hover:true, multiselect:true, tooltipDelay:120 },
  nodes: { shape:"dot", size:14, font:{ color:"#e6e9ef", size:13 },
           borderWidth:1.5 },
  edges: { arrows:{ to:{ enabled:true, scaleFactor:0.5 } }, smooth:{ type:"dynamic" },
           color:{ opacity:0.6 }, font:{ size:9, color:"#8b93a7", strokeWidth:0 } }
});

let selectedId = null;
let selectedPathIds = [], selectedPathRevision = 0, selectedPathBusy = false;
let selectedPathHighlighted = false;
let rmstEdgeHandler = null;
const status = (m) => document.getElementById("status").textContent = m || "";
const limitEl = document.getElementById("limit");
const limitVal = document.getElementById("limitVal");
limitEl.oninput = () => limitVal.textContent = limitEl.value;

function selectedTypes() {
  return [...document.querySelectorAll(".etype:checked")].map(c => c.value).join(",");
}
function nodeStyle(n) {
  const language = n.language || "en";
  return { id:n.id, label:`${n.title} [${language}]`, pageTitle:n.title, language,
           wikidataId:n.wikidata_id, nodeType:n.type, title:`${n.title} [${language}; ${n.type}]`,
           color:{ background:NODE_COLORS[n.type]||"#888", border:"#20242e" } };
}
function addNode(n) {
  if (!nodes.get(n.id)) nodes.add(nodeStyle(n));
}
function edgeId(a,b,t){ return `${a}->${b}:${t}`; }
const EDGE_WEIGHTS = __EDGE_WEIGHTS__;
const EDGE_COSTS = __EDGE_COSTS__;
const defaultSeconds = __DEFAULT_SECONDS__;
function setupSearchTime(selectId, customId, initialSeconds) {
  const select = document.getElementById(selectId);
  if (!select) return;
  for (const [seconds, label] of [[300,"5 minutes"], [900,"15 minutes"], [3600,"1 hour"],
                                [21600,"6 hours"], [86400,"24 hours"], [0,"No time limit"]]) {
    if (![...select.options].some(o => Number(o.value) === seconds)) select.add(new Option(label, String(seconds)));
  }
  if (![...select.options].some(o => Number(o.value) === initialSeconds)) {
    select.add(new Option(initialSeconds + " seconds", String(initialSeconds)));
  }
  select.add(new Option("Custom seconds...", "custom"));
  select.value = String(initialSeconds);
  const custom = document.getElementById(customId);
  select.addEventListener("change", () => {
    custom.hidden = select.value !== "custom";
    if (!custom.hidden) custom.focus();
  });
}
function searchSeconds(selectId, customId) {
  const choice = document.getElementById(selectId).value;
  const raw = choice === "custom" ? document.getElementById(customId).value.trim() : choice;
  const seconds = Number(raw);
  if (!raw || !Number.isFinite(seconds) || seconds < 0) throw new Error("Enter finite seconds >= 0; use 0 for no time limit.");
  return seconds;
}
function searchAllowance(seconds) {
  return seconds === 0 ? "no time limit" : `search allowance: ${seconds} seconds`;
}
setupSearchTime("pfTimeout", "pfCustomSeconds", defaultSeconds);
setupSearchTime("rmstSeconds", "rmstCustomSeconds", 30);
const NORMAL_EDGE_FONT = { size:9, color:"#8b93a7", strokeWidth:0 };
let highlightedEdgeIds = new Set();

function edgeTooltip(type, weight) {
  const c = EDGE_COSTS[type] || { fwd:"?", rev:"?" };
  return `${type}: forward cost ${c.fwd}, reverse cost ${c.rev} ` +
         `(relevance weight ${weight}) - click edge for provenance`;
}
function baseEdgeData(id, from, to, type, weight, pathOnly=false) {
  return { id, from, to, etype:type, storedWeight:weight, pathOnly, width:1,
           color:{ color:EDGE_COLORS[type], opacity:0.6 },
           label:Number(weight) === 0 ? "0" : "",
           title:edgeTooltip(type, weight), dashes:Number(weight) === 0,
           font:NORMAL_EDGE_FONT };
}
function clearPathHighlights() {
  for (const eid of highlightedEdgeIds) {
    const e = edges.get(eid);
    if (!e) continue;
    if (e.pathOnly) {
      edges.remove(eid);
    } else {
      edges.update(baseEdgeData(eid, e.from, e.to, e.etype, e.storedWeight));
    }
  }
  highlightedEdgeIds.clear();
}

async function doSearch() {
  const q = document.getElementById("q").value.trim();
  const box = document.getElementById("results");
  if (!q) { box.innerHTML = ""; return; }
  const language = document.getElementById("searchLanguage").value;
  const r = await fetch("/api/search?q=" + encodeURIComponent(q) + "&language=" + encodeURIComponent(language));
  const items = await r.json();
  if (document.getElementById("q").value.trim() !== q ||
      document.getElementById("searchLanguage").value !== language) return;
  box.innerHTML = "";
  if (!items.length) { box.innerHTML = '<div class="muted">No matches.</div>'; return; }
  for (const it of items) {
    const d = document.createElement("div");
    d.className = "res";
    d.innerHTML = `<span>${esc(it.title)}</span><span><span class="badge b-language">${esc(it.language)}</span> <span class="badge b-${it.type}">${esc(it.type)}</span></span>`;
    d.onclick = () => { addNode(it); network.selectNodes([it.id]); syncSelectedPath(); onSelect(it.id);
                        expand(it.id); };
    box.appendChild(d);
  }
}
let searchTimer = null;
document.getElementById("q").addEventListener("input", () => {
  clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 220);
});
document.getElementById("searchLanguage").addEventListener("change", doSearch);

async function expand(id) {
  status("Loading neighbors...");
  const url = `/api/neighbors?id=${id}&limit=${limitEl.value}&types=${selectedTypes()}`;
  const r = await fetch(url);
  if (!r.ok) { status("Error loading node."); return; }
  const data = await r.json();
  addNode(data.node);
  let added = 0;
  for (const e of data.edges) {
    addNode(e.other);
    const a = e.dir === "out" ? id : e.other.id;
    const b = e.dir === "out" ? e.other.id : id;
    const eid = edgeId(a,b,e.type);
    const existing = edges.get(eid);
    if (!existing) {
      edges.add(baseEdgeData(eid, a, b, e.type, e.weight));
      added++;
    } else if (existing.pathOnly) {
      // It was introduced by a path; expansion makes it a normal canvas edge.
      edges.update({ id:eid, pathOnly:false, storedWeight:e.weight });
    }
  }
  const n = data.node;
  const shownOut = data.edges.filter(e=>e.dir==="out").length;
  const shownIn = data.edges.filter(e=>e.dir==="in").length;
  const moreOut = n.deg_out - shownOut, moreIn = n.deg_in - shownIn;
  status(`${n.title}: out ${n.deg_out} (shown ${shownOut}${moreOut>0?`, +${moreOut} more`:""}), ` +
         `in ${n.deg_in} (shown ${shownIn}${moreIn>0?`, +${moreIn} more`:""})`);
  onSelect(id, n);
}

function wikiUrl(title, language="en") {
  // Map a node title to its Wikipedia page URL (keeps the ":" of namespaces
  // like "Category:" and any "/" so the canonical link resolves directly).
  const t = String(title).replace(/ /g, "_");
  return "https://" + encodeURIComponent(language) + ".wikipedia.org/wiki/" +
         encodeURIComponent(t).replace(/%3A/gi, ":").replace(/%2F/gi, "/");
}

async function onSelect(id, meta) {
  selectedId = id;
  const node = nodes.get(id);
  if (!node) return;
  document.getElementById("selPanel").style.display = "block";
  document.getElementById("selTitle").textContent = node.label;
  const wiki = document.getElementById("btnWiki");
  wiki.href = wikiUrl(node.pageTitle, node.language);
  wiki.title = wiki.href;
  if (!meta) {
    const r = await fetch(`/api/neighbors?id=${id}&limit=1`);
    if (r.ok) meta = (await r.json()).node;
  }
  document.getElementById("selMeta").textContent = meta
    ? `${meta.language} | outgoing: ${meta.deg_out.toLocaleString()} | incoming: ${meta.deg_in.toLocaleString()}`
    : "";
}

network.on("click", (p) => {
  const hitNode = p.pointer?.DOM ? network.getNodeAt(p.pointer.DOM) : p.nodes[0];
  const hitEdge = p.pointer?.DOM ? network.getEdgeAt(p.pointer.DOM) : p.edges[0];
  if (hitNode != null) { syncSelectedPath(); onSelect(hitNode); }
  else if (hitEdge != null) {
    const edge = edges.get(hitEdge);
    if (edge && edge.rmstIndex != null && rmstEdgeHandler) rmstEdgeHandler(edge.rmstIndex);
    else showProvenance(hitEdge);
  }
  else syncSelectedPath();
});
network.on("selectNode", syncSelectedPath);
network.on("deselectNode", p => { if (p.nodes.length || !p.edges.length) syncSelectedPath(); });
network.on("doubleClick", (p) => {
  const id = p.pointer?.DOM ? network.getNodeAt(p.pointer.DOM) : p.nodes[0];
  if (id != null) expand(id);
});

function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
async function showProvenance(eid) {
  const e = edges.get(eid);
  if (!e || !e.etype) return;
  const panel = document.getElementById("provPanel");
  const body = document.getElementById("provBody");
  panel.style.display = "block";
  document.getElementById("provHead").textContent = "loading...";
  body.innerHTML = "";
  const r = await fetch(`/api/provenance?src=${e.from}&dst=${e.to}&type=${e.etype}`);
  const d = await r.json();
  if (d.error) { document.getElementById("provHead").textContent = d.error; return; }
  document.getElementById("provHead").innerHTML =
    `<b>${esc(d.src)}</b> <span class="muted">[${esc(d.src_language)}; ${esc(d.src_type)}]</span> ` +
    `&mdash;${esc(d.type)}&rarr; <b>${esc(d.dst)}</b> <span class="muted">[${esc(d.dst_language)}; ${esc(d.dst_type)}]</span>`;
  let hml = "";
  if (d.lines && d.lines.length) {
    hml += '<div class="muted" style="margin:4px 0">Source wikitext line(s) in "' +
           esc(d.src) + '":</div>';
    for (const ln of d.lines) {
      hml += `<pre style="white-space:pre-wrap; background:#0e1116; border:1px solid #2a2f3a;
              border-radius:6px; padding:6px; margin:4px 0; font-size:11px;">${esc(ln)}</pre>`;
    }
  }
  for (const evidence of d.evidence || []) {
    const qid = evidence.wikidata_id || evidence.qid;
    if (qid && /^Q[1-9][0-9]*$/.test(qid)) {
      hml += `<div>Shared Wikidata item: <a href="https://www.wikidata.org/wiki/${qid}" target="_blank" rel="noopener noreferrer">${qid}</a></div>`;
    }
    hml += `<pre style="white-space:pre-wrap; overflow-wrap:anywhere; font-size:11px">${esc(JSON.stringify(evidence, null, 2))}</pre>`;
  }
  if (d.note) hml += `<div class="muted" style="margin-top:4px">${esc(d.note)}</div>`;
  body.innerHTML = hml;
}

