// ---- retrieved-set minimum spanning tree ---- //
if (RMST_ENABLED) {
let rmstPicked = null, rmstLast = null, rmstRevision = 0, rmstBusy = false;
const rmstList = document.getElementById("rmstArticles");
const rmstResult = document.getElementById("rmstResult");
const rmstPreview = document.getElementById("rmstPreview");
makePicker(document.getElementById("rmstSearch"), document.getElementById("rmstSearchRes"),
           document.getElementById("rmstLanguage"), id => rmstPicked = id, true);

function rmstChanged() {
  rmstRevision++;
  rmstLast = null;
  rmstResult.textContent = "";
  rmstPreview.textContent = "List changed. Check it before calculating.";
  for (const edge of edges.get()) if (edge.rmstIndex != null) edges.remove(edge.id);
}
routePolicyListeners.push(rmstChanged);
for (const id of ["rmstArticles", "rmstK", "rmstLanguage", "rmstSeconds", "rmstCustomSeconds", "rmstExhaustive"]) {
  document.getElementById(id).addEventListener("input", rmstChanged);
  document.getElementById(id).addEventListener("change", rmstChanged);
}
function rmstPayload() {
  return { retrieved_articles:rmstList.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
           top_k:Number(document.getElementById("rmstK").value),
           language:document.getElementById("rmstLanguage").value || BASE_LANGUAGE,
           seconds:searchSeconds("rmstSeconds", "rmstCustomSeconds"),
           exhaustive:document.getElementById("rmstExhaustive").checked, policy:routePolicy() };
}
async function rmstPost(path, payload) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "Calculation request failed");
  return data;
}
function rmstSetBusy(busy) {
  rmstBusy = busy;
  for (const id of ["rmstCheck", "rmstGo"]) document.getElementById(id).disabled = busy;
}
async function rmstAdd(id) {
  if (id == null) { status("Choose an article first."); return; }
  const response = await fetch(`/api/neighbors?id=${id}&limit=1`);
  if (!response.ok) { status("The selected article could not be loaded."); return; }
  const node = (await response.json()).node;
  if (node.type !== "article") { status("R-MST terminals must be articles. Choose an article node."); return; }
  rmstList.value = [rmstList.value.trim(), `${node.language}:${node.title}`].filter(Boolean).join("\n");
  rmstChanged();
  document.getElementById("rmstPanel").open = true;
  document.getElementById("rmstPanel").scrollIntoView({block:"nearest"});
}
document.getElementById("rmstAddSearch").onclick = () => rmstAdd(rmstPicked).catch(error => status(error.message));
document.getElementById("rmstAddSelected").onclick = () => rmstAdd(selectedId).catch(error => status(error.message));
document.getElementById("btnRmst").onclick = () => rmstAdd(selectedId).catch(error => status(error.message));

function renderRmstPreview(data) {
  let html = `<div>${data.included_count} ranked entries within top-${data.top_k}; <b>${data.unique_article_count} unique articles</b>.</div>`;
  if (data.input_count > data.included_count) html += '<div class="muted">Entries after top-k are excluded.</div>';
  for (const entry of data.entries) {
    const text = entry.node ? `${entry.node.language}:${entry.node.title}` : String(entry.requested);
    const detail = entry.status === "duplicate" ? " — duplicate; counted once" : entry.reason ? ` — ${entry.reason}` : "";
    html += `<div class="rmst-entry ${entry.status === "unresolved" ? "rmst-problem" : "muted"}">${entry.rank}. ${esc(text + detail)}</div>`;
  }
  if (!data.ready) html += '<div class="rmst-problem">Correct unresolved entries before calculating.</div>';
  rmstPreview.innerHTML = html;
}
document.getElementById("rmstCheck").onclick = async () => {
  const revision = rmstRevision;
  rmstSetBusy(true);
  rmstPreview.textContent = "Checking article titles...";
  try {
    const data = await rmstPost("/api/rmst/resolve", rmstPayload());
    if (revision === rmstRevision) renderRmstPreview(data);
  } catch (error) { if (revision === rmstRevision) rmstPreview.textContent = error.message; }
  finally { rmstSetBusy(false); }
};

function renderRmst(data) {
  let html = data.certified
    ? `<div><b>R-MST: ${Number(data.mean_edge_cost).toFixed(4)}</b></div><div>Total cost ${data.total_cost} · ${data.unique_article_count} unique articles · ${data.edges.length} tree edges</div>`
    : `<div class="rmst-problem"><b>${data.status === "disconnected" ? "Disconnected in the loaded graph" : "Incomplete calculation"}</b> — a complete R-MST score is unavailable.</div>`;
  if (!data.certified) html += `<div>${data.components.length} component(s); ${data.available_pair_count || 0}/${data.expected_pair_count || 0} pairs have path witnesses.</div>`;
  if (data.limit_reasons?.length) html += `<div class="muted">Search limits: ${esc(data.limit_reasons.join(", "))}.</div>`;
  html += `<div class="muted">${Number(data.elapsed_seconds).toFixed(3)} seconds. ${data.dataset.bounded ? "Bounded imported sample." : "Loaded graph."}</div>`;
  if (data.edges.length || data.terminals.length) html += '<button class="btn" id="rmstShowTree" style="margin-top:7px">Show tree / forest</button>';
  html += '<div class="muted" style="margin:7px 0">Click a tree edge or a path button to inspect its Wikipedia route.</div>';
  for (let index = 0; index < data.edges.length; index++) {
    const edge = data.edges[index], first = edge.path[0], last = edge.path[edge.path.length - 1];
    html += `<div class="rmst-entry">${esc(first.title)} [${esc(first.language)}] ↔ ${esc(last.title)} [${esc(last.language)}]: ${edge.distance}${edge.approx ? " (path upper bound)" : ""} <button class="btn" data-rmst-path="${index}">Inspect path</button></div>`;
  }
  rmstResult.innerHTML = html;
  document.getElementById("rmstShowTree")?.addEventListener("click", showRmstTree);
  for (const button of rmstResult.querySelectorAll("[data-rmst-path]")) button.onclick = () => showRmstPath(Number(button.dataset.rmstPath));
}
function clearRmstCanvas() {
  nodes.clear(); edges.clear(); highlightedEdgeIds.clear(); selectedId = null;
  setSelectedPath([]);
  document.getElementById("selPanel").style.display = "none";
  document.getElementById("provPanel").style.display = "none";
}
function showRmstTree() {
  if (!rmstLast) return;
  clearRmstCanvas();
  for (const node of rmstLast.terminals) addNode(node);
  rmstLast.edges.forEach((edge, index) => edges.add({id:`rmst:${index}`, from:edge.source_id, to:edge.target_id,
    rmstIndex:index, label:String(edge.distance), width:3, dashes:edge.approx,
    arrows:{to:{enabled:false}}, color:{color:"#ac7de8", opacity:1},
    font:{color:"#d5b7ff", size:12, strokeWidth:0}, title:"Terminal connection — click to inspect the Wikipedia path"}));
  network.fit({animation:true});
  status("R-MST terminal tree / forest. Each edge represents a Wikipedia path.");
}
function showRmstPath(index) {
  const edge = rmstLast?.edges[index];
  if (!edge) return;
  clearRmstCanvas();
  for (const node of edge.path) addNode(node);
  for (const step of edge.steps) {
    const from = step.direction === "fwd" ? step.from_id : step.to_id;
    const to = step.direction === "fwd" ? step.to_id : step.from_id;
    const id = edgeId(from, to, step.type);
    edges.update({...baseEdgeData(id, from, to, step.type, EDGE_WEIGHTS[step.type] ?? 0, true),
      label:`${step.rel} (${step.cost})`, width:3, color:{color:"#f05a7a", opacity:1}});
    highlightedEdgeIds.add(id);
  }
  network.fit({animation:true});
  status(`Tree-edge path: cost ${edge.distance}; ${edge.hops} hops. Click a relationship for provenance.`);
  const existing = document.getElementById("rmstPathDetail");
  if (existing) existing.remove();
  const detail = document.createElement("div");
  detail.id = "rmstPathDetail";
  detail.style.marginTop = "8px";
  detail.innerHTML = '<b>Selected path</b>' + edge.path.map((node, i) =>
    `<div class="rmst-entry">${i ? `<span class="muted">${esc(edge.steps[i-1].rel)} (+${edge.steps[i-1].cost})${edge.steps[i-1].wikidata_id ? "; " + esc(edge.steps[i-1].wikidata_id) : ""}</span><br/>` : ""}${esc(node.title)} [${esc(node.language)}]</div>`).join("");
  rmstResult.appendChild(detail);
}
document.getElementById("rmstGo").onclick = async () => {
  const revision = rmstRevision;
  let payload;
  try { payload = rmstPayload(); }
  catch (error) { rmstResult.textContent = error.message; return; }
  rmstSetBusy(true);
  rmstLast = null;
  rmstResult.textContent = "Checking article list...";
  try {
    const preview = await rmstPost("/api/rmst/resolve", payload);
    if (revision !== rmstRevision) return;
    renderRmstPreview(preview);
    if (!preview.ready) { rmstResult.textContent = "Fix the article list to continue."; return; }
    rmstResult.textContent = `Calculating pairwise paths and R-MST (${searchAllowance(payload.seconds)})...`;
    const data = await rmstPost("/api/rmst", payload);
    if (revision !== rmstRevision) return;
    rmstLast = data;
    renderRmst(data);
    showRmstTree();
  } catch (error) { if (revision === rmstRevision) rmstResult.textContent = error.message; }
  finally { rmstSetBusy(false); }
};

rmstEdgeHandler = showRmstPath;
}

