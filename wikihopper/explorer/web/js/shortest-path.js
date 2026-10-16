// ---- distance / shortest path ---- //
let pfFromId = null, pfToId = null, pfRevision = 0;
function pfChanged() {
  pfRevision++;
  clearPathHighlights();
  document.getElementById("pfResult").textContent = "Search settings changed. Find a path again.";
}
for (const id of ["pfFrom", "pfTo", "pfFromLanguage", "pfToLanguage", "pfTimeout", "pfCustomSeconds", "pfExhaustive"]) {
  document.getElementById(id).addEventListener("input", pfChanged);
  document.getElementById(id).addEventListener("change", pfChanged);
}
function makePicker(inputEl, resEl, languageEl, setId, articleOnly=false) {
  let timer = null;
  let version = 0;
  function search() {
    const currentVersion = ++version;
    setId(null);
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inputEl.value.trim();
      if (!q) { resEl.innerHTML = ""; return; }
      const items = await (await fetch("/api/search?q=" + encodeURIComponent(q) +
                                     "&language=" + encodeURIComponent(languageEl.value))).json();
      if (currentVersion !== version) return;
      resEl.innerHTML = "";
      for (const it of items) {
        if (articleOnly && it.type !== "article") continue;
        const d = document.createElement("div");
        d.className = "res";
        d.innerHTML = `<span>${esc(it.title)}</span><span><span class="badge b-language">${esc(it.language)}</span> <span class="badge b-${it.type}">${esc(it.type)}</span></span>`;
        d.onclick = () => { setId(it.id); inputEl.value = it.title; resEl.innerHTML = ""; };
        resEl.appendChild(d);
      }
    }, 220);
  }
  inputEl.addEventListener("input", search);
  languageEl.addEventListener("change", search);
}
makePicker(document.getElementById("pfFrom"), document.getElementById("pfFromRes"),
           document.getElementById("pfFromLanguage"), id => { pfFromId = id; pfChanged(); });
makePicker(document.getElementById("pfTo"), document.getElementById("pfToRes"),
           document.getElementById("pfToLanguage"), id => { pfToId = id; pfChanged(); });

document.getElementById("pfUseSel").onclick = () => {
  if (selectedId == null) { status("Select a node first."); return; }
  pfFromId = selectedId;
  pfChanged();
  const n = nodes.get(selectedId);
  document.getElementById("pfFrom").value = n ? n.pageTitle : selectedId;
  document.getElementById("pfFromLanguage").value = n ? n.language : "";
};

document.getElementById("pfUseSelTo").onclick = () => {
  if (selectedId == null) { status("Select a node first."); return; }
  pfToId = selectedId;
  pfChanged();
  const n = nodes.get(selectedId);
  document.getElementById("pfTo").value = n ? n.pageTitle : selectedId;
  document.getElementById("pfToLanguage").value = n ? n.language : "";
};

document.getElementById("pfGo").onclick = async () => {
  const res = document.getElementById("pfResult");
  if (pfFromId == null || pfToId == null) { res.textContent = "Pick both a From and a To node."; return; }
  let seconds, policy;
  try { seconds = searchSeconds("pfTimeout", "pfCustomSeconds"); policy = routePolicy(); }
  catch (error) { res.textContent = error.message; return; }
  const exhaustive = document.getElementById("pfExhaustive").checked ? 1 : 0;
  const revision = ++pfRevision;
  clearPathHighlights();
  res.textContent = `Searching (${searchAllowance(seconds)})...`;
  const params = new URLSearchParams({src:pfFromId, dst:pfToId, seconds, exhaustive,
    allowed_languages:policy.allowed_languages.join(","), allowed_edge_types:policy.allowed_edge_types.join(","),
    ambiguity_guard:String(policy.ambiguity_guard)});
  let d;
  try { d = await (await fetch(`/api/path?${params}`)).json(); }
  catch (error) { if (revision === pfRevision) res.textContent = error.message; return; }
  if (revision !== pfRevision) return;
  if (d.error) { res.textContent = d.error; return; }
  const elapsed = Number(d.elapsed_seconds || 0).toFixed(3);
  if (!d.found) {
    res.innerHTML = `<span class="muted">${esc(d.note || "No path found.")} (${elapsed}s)</span>`;
    return;
  }
  let h = `<div><b>Distance ${d.approx ? "\u2264 " : ""}${d.distance}</b> &middot; ${d.hops} hop(s) ` +
          `<span class="muted">(${d.settled.toLocaleString()} nodes explored in ${elapsed}s)</span></div>`;
  if (d.approx) {
    const reason = [d.limit_reason, d.hub_pruned ? "hub pruning" : null].filter(Boolean).join(" and ");
    h += `<div class="muted" style="margin-top:2px">Search used ${esc(reason)}; distance is an upper bound (a shorter route may exist).</div>`;
  }
  h += '<div style="margin-top:6px">';
  h += `<div>&bull; ${esc(d.path[0].title)} <span class="muted">[${esc(d.path[0].language)}; ${esc(d.path[0].type)}]</span></div>`;
  for (let i = 0; i < d.steps.length; i++) {
    const s = d.steps[i], nx = d.path[i + 1];
    const identity = s.wikidata_id ? `; ${esc(s.wikidata_id)}` : "";
    h += `<div style="margin-left:8px" class="muted">&darr; ${esc(s.rel)} (+${s.cost}${identity})</div>`;
    h += `<div>&bull; ${esc(nx.title)} <span class="muted">[${esc(nx.language)}; ${esc(nx.type)}]</span></div>`;
  }
  h += "</div>";
  res.innerHTML = h;
  drawPath(d);
};

function drawPath(d) {
  clearPathHighlights();
  selectedPathHighlighted = false;
  for (const n of d.path) addNode(n);
  for (let i = 0; i < d.steps.length; i++) {
    const s = d.steps[i], a = d.path[i].id, b = d.path[i + 1].id;
    const from_ = s.direction === "fwd" ? a : b, to_ = s.direction === "fwd" ? b : a;
    const eid = edgeId(from_, to_, s.type);
    const existing = edges.get(eid);
    const weight = EDGE_WEIGHTS[s.type] ?? 0;
    const pathOnly = existing ? Boolean(existing.pathOnly) : true;
    if (!existing) edges.add(baseEdgeData(eid, from_, to_, s.type, weight, true));
    const c = EDGE_COSTS[s.type] || { fwd:"?", rev:"?" };
    edges.update({ id:eid, from:from_, to:to_, etype:s.type,
                   storedWeight:weight, pathOnly,
                   label:`${s.rel} (${s.cost})`, width:3,
                   title:`${s.type}: ${s.rel}; this traversal costs ${s.cost}; ` +
                         `forward cost ${c.fwd}, reverse cost ${c.rev} ` +
                         `(relevance weight ${weight}) - click edge for provenance`,
                   color:{ color:"#f05a7a", opacity:1 }, dashes:s.cost === 0,
                   font:{ color:"#ffb3c1", size:11, strokeWidth:0 } });
    highlightedEdgeIds.add(eid);
  }
  network.fit({ nodes: d.path.map(n => n.id), animation: true });
}

