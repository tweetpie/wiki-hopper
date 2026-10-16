// ---- measure an ordered route through selected canvas nodes ---- //
function renderSelectedPath() {
  const list = document.getElementById("selectedPathList");
  list.innerHTML = selectedPathIds.map((id, index) => {
    const n = nodes.get(id);
    return `<li>${esc(n.pageTitle)} <span class="muted">[${esc(n.language)}]</span> ` +
      `<button class="btn" data-route-move="-1" data-route-index="${index}" aria-label="Move node ${index + 1} earlier" ${index === 0 ? "disabled" : ""}>↑</button>` +
      `<button class="btn" data-route-move="1" data-route-index="${index}" aria-label="Move node ${index + 1} later" ${index === selectedPathIds.length - 1 ? "disabled" : ""}>↓</button>` +
      `<button class="btn" data-route-remove="${index}" aria-label="Remove node ${index + 1} from route">×</button></li>`;
  }).join("");
  document.getElementById("selectedPathTest").disabled = selectedPathIds.length < 2 || selectedPathBusy;
  document.getElementById("selectedPathReverse").disabled = selectedPathIds.length < 2;
  document.getElementById("selectedPathClear").disabled = selectedPathIds.length === 0;
}

function setSelectedPath(ids, selectOnCanvas=false) {
  ids = [...new Set(ids)].filter(id => nodes.get(id));
  if (ids.length === selectedPathIds.length && ids.every((id, index) => id === selectedPathIds[index])) return;
  selectedPathIds = ids;
  selectedPathRevision++;
  if (selectedPathHighlighted) {
    clearPathHighlights();
    selectedPathHighlighted = false;
  }
  if (selectOnCanvas) network.selectNodes(ids, false);
  document.getElementById("selectedPathResult").textContent = ids.length < 2
    ? "Select at least two nodes." : `${ids.length} nodes in route order. Test to measure ${ids.length - 1} hops.`;
  renderSelectedPath();
}

function syncSelectedPath() {
  const selected = network.getSelectedNodes();
  const keep = new Set(selected);
  // Retain click order; vis may enumerate its selected-node set by node ID.
  setSelectedPath([...selectedPathIds.filter(id => keep.has(id)),
                   ...selected.filter(id => !selectedPathIds.includes(id))]);
}

document.getElementById("selectedPathList").onclick = event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  const ids = [...selectedPathIds];
  if (button.dataset.routeRemove != null) ids.splice(Number(button.dataset.routeRemove), 1);
  else {
    const index = Number(button.dataset.routeIndex), next = index + Number(button.dataset.routeMove);
    [ids[index], ids[next]] = [ids[next], ids[index]];
  }
  setSelectedPath(ids, true);
};
document.getElementById("selectedPathReverse").onclick = () => setSelectedPath([...selectedPathIds].reverse(), true);
document.getElementById("selectedPathClear").onclick = () => setSelectedPath([], true);
document.getElementById("selectedPathTest").onclick = async () => {
  if (selectedPathIds.length < 2 || selectedPathBusy) return;
  const revision = selectedPathRevision, ids = [...selectedPathIds];
  const result = document.getElementById("selectedPathResult");
  selectedPathBusy = true;
  renderSelectedPath();
  result.textContent = "Testing selected route...";
  try {
    const policy = routePolicy();
    const response = await fetch("/api/path/test", {method:"POST", headers:{"Content-Type":"application/json"},
                                                 body:JSON.stringify({node_ids:ids, policy})});
    const d = await response.json();
    if (revision !== selectedPathRevision) return;
    if (!response.ok) throw new Error(d.error || "Could not test the selected path");
    if (!d.valid) {
      result.textContent = d.note;
      return;
    }
    let html = `<div><b>Selected length ${d.distance}</b> · ${d.hops} hop(s)</div>`;
    for (const step of d.steps) {
      html += `<div class="muted" style="margin-top:4px">${esc(step.from)} [${esc(step.from_language)}] → ` +
              `${esc(step.to)} [${esc(step.to_language)}]: ${esc(step.rel)} (+${step.cost})</div>`;
    }
    result.innerHTML = html;
    drawPath(d);
    selectedPathHighlighted = true;
    network.selectNodes(ids, false);
    status(`Selected route: length ${d.distance}; ${d.hops} hops. Click a relationship for provenance.`);
  } catch (error) {
    if (revision === selectedPathRevision) result.textContent = error.message;
  } finally {
    selectedPathBusy = false;
    renderSelectedPath();
  }
};

