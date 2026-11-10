// Shared helpers for every page.

/** Build a DOM element: el("a", {href: "/x", class: "y"}, "text", childNode, ...) */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

const DIFFICULTY_LABEL = { low: "Low difficulty", medium: "Medium difficulty", high: "High difficulty" };

/** Chips showing industry / TRL / difficulty for a technology. */
function techChips(t) {
  return el("div", { class: "chips" },
    t.industry && el("span", { class: "chip" }, t.industry),
    t.trl && el("span", { class: "chip plain", title: "Technology Readiness Level (1-9)" }, `TRL ${t.trl}`),
    t.difficulty && el("span", { class: `chip ${t.difficulty}` }, DIFFICULTY_LABEL[t.difficulty]),
    t.licensing_status === "no longer listed" && el("span", { class: "chip high" }, "No longer listed"),
    !t.summary && el("span", { class: "chip plain" }, "AI summary pending"),
  );
}

/** A result card used by both keyword and reverse search. */
function techCard(t) {
  const href = `/tech.html?id=${t.id}`;
  const body = t.summary
    ? el("p", {}, t.summary)
    : el("p", { class: "snippet" }, (t.abstract_snippet || "").trim() + "…");
  return el("article", { class: "card" },
    t.score !== undefined && el("span", { class: "score", title: "Similarity to your problem" },
      `${Math.round(t.score * 100)}% match`),
    el("h3", {}, el("a", { href }, t.title)),
    el("div", { class: "inst" }, t.institution_name, t.published_date ? ` · ${t.published_date}` : ""),
    body,
    techChips(t),
  );
}
