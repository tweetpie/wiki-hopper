const NODE_COLORS = { article:"#4a90e2", category:"#40c07d", disambiguation:"#e0a63a" };
const EDGE_COLORS = { HAS_CATEGORY:"#40c07d", SUBCATEGORY_OF:"#2f8f63",
                      HAS_DISAMBIGUATION:"#e0a63a", DISAMBIGUATES_TO:"#c98a2a",
                      LANGUAGE_EQUIVALENT:"#ac7de8" };
const LANGUAGES = __LANGUAGES__;
const BASE_LANGUAGE = __BASE_LANGUAGE__;
const RMST_ENABLED = __RMST_ENABLED__;
const routePolicyListeners = [];
let routeLanguagesReady = false;
document.getElementById("routeAmbiguityGuard").checked = __AMBIGUITY_GUARD__;
function routePolicy() {
  if (!routeLanguagesReady) throw new Error("Loaded route languages are not available yet.");
  const allowed_languages = [...document.querySelectorAll(".routeLanguage:checked")].map(el => el.value);
  const allowed_edge_types = [...document.querySelectorAll(".routeEdge:checked")].flatMap(el => el.value.split(","));
  if (!allowed_languages.length) throw new Error("Select at least one Route language.");
  if (!allowed_edge_types.length) throw new Error("Select at least one Route relationship.");
  return {allowed_languages, allowed_edge_types,
          ambiguity_guard:document.getElementById("routeAmbiguityGuard").checked};
}
function routePolicyChanged() {
  pfRevision++;
  selectedPathRevision++;
  clearPathHighlights();
  selectedPathHighlighted = false;
  document.getElementById("pfResult").textContent = "Route settings changed. Find a path again.";
  document.getElementById("selectedPathResult").textContent = "Route settings changed. Test the selected route again.";
  for (const listener of routePolicyListeners) listener();
}
document.getElementById("routePolicyPanel").addEventListener("change", routePolicyChanged);
for (const id of ["searchLanguage", "pfFromLanguage", "pfToLanguage", "rmstLanguage"]) {
  const select = document.getElementById(id);
  if (!select) continue;
  if (id !== "rmstLanguage") select.add(new Option("All languages", ""));
  for (const [code, details] of Object.entries(LANGUAGES)) {
    select.add(new Option(`${details.name} (${code})`, code));
  }
  select.value = id === "searchLanguage" ? "" : BASE_LANGUAGE;
}
async function loadDatasetInfo() {
  const banner = document.getElementById("datasetInfo");
  try {
    const response = await fetch("/api/dataset");
    if (!response.ok) throw new Error("Dataset metadata unavailable");
    const dataset = await response.json();
    const counts = dataset.language_counts || {};
    const routeLanguages = document.getElementById("routeLanguages");
    routeLanguages.replaceChildren();
    for (const code of Object.keys(counts).sort()) {
      const label = document.createElement("label");
      label.className = "chk";
      const input = document.createElement("input");
      input.type = "checkbox"; input.className = "routeLanguage";
      input.value = code; input.checked = true;
      label.appendChild(input);
      label.appendChild(document.createTextNode(` ${LANGUAGES[code]?.name || code} (${code})`));
      routeLanguages.appendChild(label);
    }
    routeLanguagesReady = true;
    const label = code => LANGUAGES[code]?.name ? `${LANGUAGES[code].name} (${code})` : code;
    const loaded = Object.entries(counts).map(([code, count]) =>
      `    ${label(code)}: ${Number(count).toLocaleString()} nodes`);
    const scope = dataset.bounded === true ? "Loaded dataset (bounded sample)." : "Loaded dataset.";
    banner.textContent = scope + "\nLoaded languages:\n" + (loaded.join("\n") || "    none");
    for (const id of ["searchLanguage", "pfFromLanguage", "pfToLanguage", "rmstLanguage"]) {
      const select = document.getElementById(id);
      if (!select) continue;
      const previous = select.value;
      select.replaceChildren();
      if (id !== "rmstLanguage") select.add(new Option("All languages", ""));
      for (const code of Object.keys(counts)) select.add(new Option(label(code), code));
      select.value = counts[previous] || (previous === "" && id !== "rmstLanguage")
        ? previous : counts[BASE_LANGUAGE] ? BASE_LANGUAGE : Object.keys(counts)[0] || "";
    }
  } catch (error) {
    banner.textContent = "Dataset information unavailable.";
  }
}
loadDatasetInfo();

