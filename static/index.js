// Home page: keyword search with filters, plus the "Describe your problem" reverse search.

const state = { page: 1, pageSize: 20 };
const $ = (id) => document.getElementById(id);

// ---- Tabs -----------------------------------------------------------------
function showTab(name) {
  for (const tab of ["search", "match"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`panel-${tab}`).hidden = tab !== name;
    $(`${tab}-form`).hidden = tab !== name;   // the hero shows the matching form
  }
  history.replaceState(null, "", name === "match" ? "#match" : location.pathname + location.search);
}
$("tab-search").onclick = () => showTab("search");
$("tab-match").onclick = () => showTab("match");
// The footer's "Describe your problem" link changes only the #hash when you're already on this page.
window.addEventListener("hashchange", () => showTab(location.hash === "#match" ? "match" : "search"));

// ---- Loading placeholders ------------------------------------------------
/** Gray shimmering cards shown while results load. */
function skeletonCards(n = 3) {
  return Array.from({ length: n }, () => el("div", { class: "card skeleton", "aria-hidden": "true" },
    el("div", { class: "sk sk-title" }),
    el("div", { class: "sk sk-meta" }),
    el("div", { class: "sk sk-line" }), el("div", { class: "sk sk-line" }), el("div", { class: "sk sk-line short" })));
}

/** Show placeholders in `container` only if loading takes long enough to notice; returns a cancel function. */
function showSkeletonsSoon(container) {
  const timer = setTimeout(() => container.replaceChildren(...skeletonCards()), 150);
  return () => clearTimeout(timer);
}

// ---- Filters --------------------------------------------------------------
async function loadFacets() {
  const f = await api("/api/facets");
  for (const i of f.institutions) {
    $("f-institution").append(el("option", { value: i.value }, `${i.name} (${i.count})`));
  }
  for (const i of f.industries) {
    $("f-industry").append(el("option", { value: i.value }, `${i.value} (${i.count})`));
  }
  const s = f.stats;
  $("stats-line").textContent = s.total
    ? `${s.total} technologies from ${f.institutions.length} research institutions · ${s.enriched} with AI business summaries`
    : "No technologies yet. Run `python -m techbridge scrape` to load some.";
  state.semantic = f.semantic_search;
}

for (const id of ["f-trl-min", "f-trl-max"]) {
  $(id).append(el("option", { value: "" }, "Any"));
  for (let n = 1; n <= 9; n++) $(id).append(el("option", { value: n }, n));
}

function currentParams() {
  const params = new URLSearchParams();
  const add = (key, value) => value && params.set(key, value);
  add("q", $("q").value.trim());
  add("institution", $("f-institution").value);
  add("industry", $("f-industry").value);
  add("difficulty", $("f-difficulty").value);
  add("status", $("f-status").value);
  add("trl_min", $("f-trl-min").value);
  add("trl_max", $("f-trl-max").value);
  return params;
}

// Restore filters from the URL so searches are linkable / survive back-navigation.
function restoreParams() {
  const p = new URLSearchParams(location.search);
  $("q").value = p.get("q") || "";
  for (const [key, id] of [["institution", "f-institution"], ["industry", "f-industry"],
                           ["difficulty", "f-difficulty"], ["status", "f-status"], ["trl_min", "f-trl-min"], ["trl_max", "f-trl-max"]]) {
    $(id).value = p.get(key) || "";
  }
  state.page = Number(p.get("page")) || 1;
}

// ---- Search ---------------------------------------------------------------
async function runSearch() {
  const params = currentParams();
  if (state.page > 1) params.set("page", state.page);
  history.replaceState(null, "", params.toString() ? `?${params}` : location.pathname);

  params.set("page", state.page);
  params.set("page_size", state.pageSize);

  $("result-count").textContent = "Searching…";
  const cancelSkeletons = showSkeletonsSoon($("results"));
  try {
    const data = await api(`/api/technologies?${params}`);

    $("result-count").textContent = `${data.total} technolog${data.total === 1 ? "y" : "ies"}`;
    $("results").replaceChildren(
      ...(data.results.length ? data.results.map(techCard)
        : [el("div", { class: "empty" }, "No technologies match. Try fewer words or clear the filters.")]));
    renderPager(data);
  } catch (err) {
    $("result-count").textContent = "";
    $("pager").replaceChildren();
    $("results").replaceChildren(
      el("div", { class: "empty" }, "Couldn't load results. Check your connection and try again."));
  } finally {
    cancelSkeletons();
  }
}

function renderPager({ total, page, page_size }) {
  const pages = Math.ceil(total / page_size);
  if (pages <= 1) return $("pager").replaceChildren();
  const go = (p) => { state.page = p; runSearch(); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const prev = el("button", { disabled: page <= 1 }, "← Previous");
  const next = el("button", { disabled: page >= pages }, "Next →");
  prev.onclick = () => go(page - 1);
  next.onclick = () => go(page + 1);
  $("pager").replaceChildren(prev, el("span", {}, `Page ${page} of ${pages}`), next);
}

$("search-form").onsubmit = (e) => { e.preventDefault(); state.page = 1; runSearch(); };
for (const id of ["f-institution", "f-industry", "f-difficulty", "f-status", "f-trl-min", "f-trl-max"]) {
  $(id).onchange = () => { state.page = 1; runSearch(); };
}
$("clear-filters").onclick = () => {
  for (const id of ["f-institution", "f-industry", "f-difficulty", "f-status", "f-trl-min", "f-trl-max"]) $(id).value = "";
  state.page = 1;
  runSearch();
};

// ---- Reverse search -------------------------------------------------------
$("match-form").onsubmit = async (e) => {
  e.preventDefault();
  const problem = $("problem").value.trim();
  if (problem.length < 3) return;
  $("match-btn").disabled = true;
  $("match-btn").textContent = "Searching…";
  $("match-mode").replaceChildren();
  const cancelSkeletons = showSkeletonsSoon($("match-results"));
  try {
    const data = await api("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ problem, limit: 10 }),
    });
    $("match-mode").replaceChildren(data.mode === "keyword"
      ? el("div", { class: "notice" }, "Semantic matching isn't set up yet (run `python -m techbridge embed`), so these are keyword matches.")
      : "");
    $("match-results").replaceChildren(
      ...(data.results.length ? data.results.map(techCard)
        : [el("div", { class: "empty" }, "No matches found. Try describing the problem differently.")]));
  } catch (err) {
    $("match-results").replaceChildren(el("div", { class: "empty" }, `Something went wrong: ${err.message}`));
  } finally {
    cancelSkeletons();
    $("match-btn").disabled = false;
    $("match-btn").textContent = "Find matches";
  }
};

// Example problems: fill the box and search right away.
for (const button of document.querySelectorAll(".example")) {
  button.onclick = () => {
    $("problem").value = button.textContent;
    $("match-form").requestSubmit();
  };
}

// ---- Start ----------------------------------------------------------------
(async () => {
  await loadFacets();
  restoreParams();
  if (location.hash === "#match") showTab("match");
  runSearch();
})();
