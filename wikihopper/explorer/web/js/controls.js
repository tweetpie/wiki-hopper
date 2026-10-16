document.getElementById("btnExpand").onclick = () => selectedId != null && expand(selectedId);
document.getElementById("btnFocus").onclick = () =>
  selectedId != null && network.focus(selectedId, { scale:1.1, animation:true });
document.getElementById("btnHide").onclick = () => {
  if (selectedId == null) return;
  const rm = edges.get().filter(e => e.from===selectedId || e.to===selectedId).map(e=>e.id);
  edges.remove(rm); nodes.remove(selectedId);
  for (const eid of rm) highlightedEdgeIds.delete(eid);
  selectedId = null; document.getElementById("selPanel").style.display = "none";
  setSelectedPath(selectedPathIds.filter(id => nodes.get(id)), true);
};
document.getElementById("btnClear").onclick = () => {
  nodes.clear(); edges.clear(); selectedId = null;
  highlightedEdgeIds.clear();
  setSelectedPath([]);
  document.getElementById("selPanel").style.display = "none"; status("");
};
