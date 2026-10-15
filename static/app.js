// Shredarr web UI. Plain JavaScript, no build step.
"use strict";

// ------------------------------------------------------------------ basics
const _fetch = window.fetch.bind(window);
window.fetch = async (...a) => {
  const r = await _fetch(...a);
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("Signed out");
  }
  return r;
};
const $ = (id) => document.getElementById(id);
const MODES = window.MODES;
const ON_SETTINGS = location.pathname === "/settings";
const rows = new Map();
const selected = new Set();
let data = null;

const icon = (n, cls) => `<svg class="i${cls ? " " + cls : ""}"><use href="#i-${n}"/></svg>`;
const size = (b) => {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < 4) (b /= 1024), i++;
  return b.toFixed(i > 2 ? 1 : 0) + " " + u[i];
};
const ruleText = (r) => {
  if (r.mode === "never") return "never delete";
  const p = [];
  if (r.mode !== "ratio" && r.min_seed_days != null) p.push(r.min_seed_days + " days");
  if (r.mode !== "days" && r.min_ratio != null) p.push("ratio " + r.min_ratio);
  return p.join(r.mode === "both" ? " and " : " or ") || "no rule";
};
const dur = (s) => {
  s = Math.max(0, s | 0);
  const d = (s / 86400) | 0,
    h = ((s % 86400) / 3600) | 0,
    m = ((s % 3600) / 60) | 0;
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${Math.max(m, 1)}m`;
};
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
};
const clock = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

function setText(el, text, cls) {
  if (el.textContent !== text) el.textContent = text;
  if (cls !== undefined && el.className !== cls) el.className = cls;
}
function updState(id, text, err) {
  setText($(id), text);
  $(id).classList.toggle("err", !!err);
}

let toastTimer;
function toast(msg, err) {
  const t = $("toast");
  t.lastChild.textContent = msg;
  t.firstChild.innerHTML = `<use href="#i-${err ? "alert" : "check"}"/>`;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = err ? "err" : ""), 3000);
}

async function postJSON(url, obj) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "X-Fetch": "1", "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
  const t = await r.text();
  if (!r.ok) throw new Error(t || r.status);
  return t;
}
const post = (url, body, type) =>
  fetch(url, { method: "POST", headers: { "X-Fetch": "1", ...(type ? { "Content-Type": type } : {}) }, body }).then((r) => {
    if (!r.ok) throw new Error(r.status);
    return r;
  });

// ------------------------------------------------------------------ theme (auto / light / dark)
const THEMES = ["auto", "light", "dark"];
const THEME_LABEL = { auto: "follows your device", light: "light", dark: "dark" };
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("htheme").firstChild.innerHTML = `<use href="#i-${{ auto: "auto", light: "sun", dark: "moon" }[t]}"/>`;
  $("htheme").title = "Theme: " + THEME_LABEL[t];
}
let theme = "auto";
try {
  theme = localStorage.getItem("shredarr-theme") || "auto";
} catch (e) {}
applyTheme(theme);
$("htheme").onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
  try {
    theme === "auto" ? localStorage.removeItem("shredarr-theme") : localStorage.setItem("shredarr-theme", theme);
  } catch (e) {}
  applyTheme(theme);
  toast("Theme: " + THEME_LABEL[theme]);
};

// ------------------------------------------------------------------ torrent rows
const CATS = {
  "movies-radarr": ["movie", "film", "Movie"],
  "tv-sonarr": ["tv", "tv", "TV"],
  radarr: ["movie", "film", "Movie"],
  sonarr: ["tv", "tv", "TV"],
  movies: ["movie", "film", "Movie"],
  tv: ["tv", "tv", "TV"],
  courses: ["course", "cap", "Course"],
};
const catOf = (c) => CATS[(c || "").toLowerCase()] || ["", "box", c || "Other"];

function state(t) {
  if (t.progress < 1)
    return ["dl", `Downloading ${(t.progress * 100).toFixed(0)}%` + (t.dlspeed ? ` · ${size(t.dlspeed)}/s` : "")];
  if (/paused|stopped/i.test(t.state)) return ["pause", "Paused"];
  if (/error|missing/i.test(t.state)) return ["err", "Error"];
  return t.upspeed > 0 ? ["up", `Seeding · ↑ ${size(t.upspeed)}/s`] : ["idle", "Seeding, idle"];
}
function status(t) {
  if (t.queued) {
    if (t.progress < 1) return ["Deleted · still downloading", "warn"];
    if (t.in_library) return [`Deleted · ${t.in_library} file${t.in_library > 1 ? "s" : ""} still in library`, "warn"];
    if (t.rule.mode === "never") return ["Deleted · protected, kept", "warn"];
    if (t.rule_met && t.delete_after && t.delete_after * 1000 > Date.now())
      return ["Deleted · deletion delay, removed in " + dur(t.delete_after - Date.now() / 1000), "warn"];
    if (t.rule_met) return [data.dry_run ? "Dry run: would delete next check" : "Deletes at next check", "ok"];
    return ["Deleted · seeding until rule is met", "warn"];
  }
  if (t.leftover) return ["Leftover · not in library", "warn"];
  return t.in_library
    ? [`In library · ${t.in_library} file${t.in_library > 1 ? "s" : ""}`, ""]
    : [t.tracked ? "Not in library" : "Not managed by Sonarr/Radarr", "mut"];
}
function setMeter(el, p, met) {
  if (p === null) return (el.className = "meter off");
  el.className = "meter" + (met ? " ok" : "");
  el.firstChild.style.width = Math.max(4, Math.min(1, p) * 100) + "%";
}
// How close a torrent is to its seeding rule: met = 2, otherwise 0..1 (the closer condition for OR rules, the further for AND)
function ruleProgress(t) {
  if (t.rule_met) return 2;
  const r = t.rule,
    p = [];
  if (r.mode === "never") return -1;
  if (r.mode !== "days" && r.min_ratio != null) p.push(r.min_ratio > 0 ? t.ratio / r.min_ratio : 1);
  if (r.mode !== "ratio" && r.min_seed_days != null) p.push(r.min_seed_days > 0 ? t.seeding_days / r.min_seed_days : 1);
  return p.length ? Math.min(1, r.mode === "both" ? Math.min(...p) : Math.max(...p)) : 1;
}

function newRow(t) {
  const tr = document.createElement("tr");
  tr.innerHTML =
    '<td class="n"><input type="checkbox" class="selbox" aria-label="Select"><div class="t"></div><div class="meta"><span class="cat"></span><span class="sz"></span><span class="del-tag"></span><span class="mo"></span></div></td>' +
    '<td class="num" data-l="Added"></td><td data-l="State"><span class="st"><span class="sd"></span><span></span></span></td>' +
    '<td class="num metric" data-l="Ratio"><div class="v"></div><div class="meter"><i></i></div></td>' +
    '<td class="num metric" data-l="Seeded"><div class="v"></div><div class="meter"><i></i></div></td>' +
    `<td class="rule" data-l="Rule"><span class="chip"></span><button class="icon" title="Edit rule" aria-label="Edit rule">${icon("edit")}</button></td>` +
    '<td data-l="Status"><span class="badge"></span></td>' +
    `<td class="act"><button class="undo" hidden title="Put it back: restore the file to your library and cancel the cleanup">${icon("undo")}<span>Undo</span></button>` +
    `<button class="icon del" title="Delete now" aria-label="Delete now">${icon("trash")}</button></td>`;
  const box = tr.querySelector(".selbox");
  box.onchange = () => {
    box.checked ? selected.add(t.hash) : selected.delete(t.hash);
    tr.classList.toggle("picked", box.checked);
    bulkBar();
  };
  tr.querySelector("td.rule button").onclick = () => editRule([t.hash]);
  tr.querySelector("button.undo").onclick = () => askUndo([t.hash]);
  tr.querySelector("button.del").onclick = () => askDelete([t.hash]);
  return tr;
}

function visible() {
  const q = $("q").value.toLowerCase(),
    cat = $("cat").value,
    show = $("show").value;
  const want = {
    queued: (t) => t.queued,
    met: (t) => t.rule_met,
    custom: (t) => t.custom,
    leftover: (t) => t.leftover,
    protected: (t) => t.rule.mode === "never",
    active: (t) => t.upspeed > 0 || t.dlspeed > 0,
  }[show];
  return data.torrents
    .filter(
      (t) =>
        !q ||
        t.name.toLowerCase().includes(q) ||
        (t.title || "").toLowerCase().includes(q) ||
        (t.queued_as || "").toLowerCase().includes(q),
    )
    .filter((t) => !cat || t.category === cat)
    .filter((t) => !want || want(t))
    .sort(cmp);
}

const lastCat = {};
function render() {
  if (data.setup) return renderSetup();
  const list = visible();
  const cats = [...new Set(data.torrents.map((t) => t.category))].sort();
  if ($("cat").options.length - 1 !== cats.length) {
    const v = $("cat").value;
    $("cat").innerHTML =
      '<option value="">All categories</option>' + cats.map((c) => `<option value="${c}">${catOf(c)[2]}</option>`).join("");
    $("cat").value = v;
  }
  const tb = $("rows");
  tb.querySelectorAll("tr.sk").forEach((x) => x.remove());
  const seen = new Set();
  list.forEach((t, i) => {
    seen.add(t.hash);
    let tr = rows.get(t.hash);
    if (!tr) rows.set(t.hash, (tr = newRow(t)));
    const td = tr.children,
      r = t.rule;
    const box = td[0].firstChild;
    if (box.checked !== selected.has(t.hash)) box.checked = selected.has(t.hash);
    tr.classList.toggle("picked", selected.has(t.hash));
    setText(td[0].children[1], t.name);
    const meta = td[0].children[2].children,
      [cc, ci, cl] = catOf(t.category);
    if (lastCat[t.hash] !== t.category) {
      meta[0].className = "cat " + cc;
      meta[0].innerHTML = icon(ci) + "<span></span>";
      meta[0].lastChild.textContent = cl;
      lastCat[t.hash] = t.category;
    }
    setText(meta[1], size(t.size));
    setText(meta[2], t.queued_as ? "deleted: " + t.queued_as : "");
    setText(meta[3], "added " + new Date(t.added_on * 1000).toLocaleDateString([], { month: "short", day: "numeric" }));
    setText(td[1], new Date(t.added_on * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }));
    const [sc, stx] = state(t),
      st = td[2].firstChild;
    setText(st.firstChild, "", "sd " + sc);
    setText(st.lastChild, stx);
    const rp =
      r.mode !== "days" && r.mode !== "never" && r.min_ratio != null ? (r.min_ratio > 0 ? t.ratio / r.min_ratio : 1) : null;
    const dp =
      r.mode !== "ratio" && r.mode !== "never" && r.min_seed_days != null
        ? r.min_seed_days > 0
          ? t.seeding_days / r.min_seed_days
          : 1
        : null;
    const mc = (m) => (m === null ? "v" : "v " + (m ? "okc" : "badc"));
    setText(td[3].firstChild, t.ratio.toFixed(2), mc(t.ratio_met));
    setMeter(td[3].lastChild, rp, t.ratio_met);
    setText(td[4].firstChild, t.seeding_days.toFixed(1) + " d", mc(t.days_met));
    setMeter(td[4].lastChild, dp, t.days_met);
    const chip = td[5].firstChild;
    const src =
      t.rule_source && !["default", "this torrent"].includes(t.rule_source) ? t.rule_source + " · " : t.custom ? "Custom · " : "";
    const key = src + ruleText(r) + t.rule_met;
    if (chip.dataset.k !== key) {
      chip.dataset.k = key;
      chip.className = "chip" + (src ? " custom" : "") + (r.mode === "never" ? " prot" : "") + (t.rule_met ? " met" : "");
      chip.innerHTML = r.mode === "never" ? icon("shield") : "";
      chip.append(src + ruleText(r));
      if (t.rule_met) chip.insertAdjacentHTML("beforeend", icon("check", "ok"));
      chip.title =
        src + ruleText(r) + " · " +
        (t.rule_source === "default" ? "default rule" : t.custom ? "this torrent's own rule" : "from the seeding rule for " + t.rule_source);
    }
    const [s, c] = status(t);
    setText(td[6].firstChild, s, "badge " + c);
    const ub = td[7].firstChild;
    if (ub.hidden !== (t.undo !== "ok")) ub.hidden = t.undo !== "ok";
    if (tb.children[i] !== tr) tb.insertBefore(tr, tb.children[i] || null);
  });
  for (const [h, tr] of rows)
    if (!seen.has(h)) {
      tr.remove();
      if (!data.torrents.some((t) => t.hash === h)) rows.delete(h), selected.delete(h);
    }
  $("empty").hidden = list.length > 0;
  $("selall").checked = list.length > 0 && list.every((t) => selected.has(t.hash));
  setText(
    $("count"),
    list.length === data.torrents.length ? String(data.torrents.length) : `${list.length}/${data.torrents.length}`,
  );
  renderStats();
  bulkBar();
}

function renderStats() {
  const up = data.torrents.filter((t) => t.upspeed > 0),
    tot = up.reduce((a, t) => a + t.upspeed, 0);
  setText($("st-total"), String(data.torrents.length));
  setText($("st-wait"), String(data.torrents.filter((t) => t.queued).length));
  setText($("st-up"), tot ? size(tot) + "/s" : "0 B/s");
  setText($("st-up-l"), `Uploading · ${up.length} torrent${up.length === 1 ? "" : "s"}`);
  if (data.freed) {
    setText($("st-freed"), size(data.freed.total));
    setText($("st-freed-l"), `Space freed · ${size(data.freed.last30)} this month`);
  }
  setText($("modet"), data.dry_run ? "Dry run" : "Live");
  $("mode").className = "pill " + (data.dry_run ? "dry" : "live");
  $("banner").hidden = !data.dry_run;
  $("dmode").options[0].text = `Default rule (${ruleText(data.default)})`;
  const bad = Object.entries(data.health || {}).filter(([, h]) => !h.ok);
  $("healthbanner").hidden = !bad.length;
  if (bad.length) setText($("healthtext"), "Can't reach " + bad.map(([k, h]) => `${NAMES[k]} (${h.msg})`).join(", ") + ".");
  const left = data.torrents.filter((t) => t.leftover);
  $("leftbanner").hidden = !left.length || $("show").value === "leftover";
  if (left.length)
    setText(
      $("lefttext"),
      `${left.length} leftover torrent${left.length === 1 ? " is" : "s are"} still seeding, but ${left.length === 1 ? "its" : "their"} media isn't in your library anymore (${size(left.reduce((a, t) => a + t.size, 0))}).`,
    );
  if (data.version) setText($("ver"), "Shredarr v" + data.version);
  const u = data.update || {};
  $("upd-link").hidden = !u.newer;
  if (u.newer) {
    $("upd-link").href = u.url;
    setText($("upd-link"), `Update available: v${u.latest}`);
  }
}
const NAMES = { sonarr: "Sonarr", radarr: "Radarr", seerr: "Seerr", qbittorrent: "qBittorrent", jellyfin: "Jellyfin" };

function renderSetup() {
  $("setupbanner").hidden = false;
  $("rows").innerHTML = "";
  $("empty").hidden = false;
  $("empty").innerHTML =
    `<img src="/logo.svg?v=4" alt=""><b>Almost there</b>Connect ${data.setup.map((k) => NAMES[k]).join(", ")} in <a href="/settings">Settings</a> to see your torrents.`;
  renderStats();
}

async function load() {
  try {
    const r = await fetch("/api/torrents");
    if (!r.ok) throw new Error(await r.text());
    data = await r.json();
    render();
    updState("upd", "Live · updated " + clock());
  } catch (e) {
    updState("upd", "Update failed: " + e.message, true);
  }
}

// ------------------------------------------------------------------ sorting
const KEYS = {
  name: (t) => t.name.toLowerCase(),
  added: (t) => t.added_on,
  state: (t) => (t.progress < 1 ? t.progress - 1 : t.upspeed),
  ratio: (t) => t.ratio,
  seeded: (t) => t.seeding_days,
  rule: ruleProgress,
  status: (t) => status(t)[0],
};
let sort = { k: null, dir: 1 };
try {
  sort = JSON.parse(localStorage.getItem("shredarr-sort")) || sort;
} catch (e) {}
function cmp(a, b) {
  if (sort.k && KEYS[sort.k]) {
    const x = KEYS[sort.k](a),
      y = KEYS[sort.k](b);
    if (x < y) return -sort.dir;
    if (x > y) return sort.dir;
  }
  return b.queued - a.queued || b.added_on - a.added_on;
}
function setSort(s) {
  sort = s;
  try {
    localStorage.setItem("shredarr-sort", JSON.stringify(sort));
  } catch (e) {}
  arrows();
  if (data) render();
}
function arrows() {
  $("msort").value = sort.k ? sort.k + ":" + sort.dir : "";
  if ($("msort").selectedIndex < 0) $("msort").value = "";
  for (const th of document.querySelectorAll("th[data-k]")) {
    th.querySelector(".arr")?.remove();
    if (th.dataset.k === sort.k) th.insertAdjacentHTML("beforeend", `<span class="arr">${sort.dir > 0 ? "▲" : "▼"}</span>`);
  }
}
for (const th of document.querySelectorAll("th[data-k]"))
  th.onclick = (e) => {
    if (e.target.closest("input")) return;
    const k = th.dataset.k,
      first = k === "rule" ? -1 : 1;
    setSort(sort.k !== k ? { k, dir: first } : sort.dir === first ? { k, dir: -first } : { k: null, dir: 1 });
  };
$("msort").onchange = () => {
  const [k, d] = $("msort").value.split(":");
  setSort(k ? { k, dir: +d } : { k: null, dir: 1 });
};
arrows();
for (const id of ["q", "cat", "show"]) $(id).addEventListener("input", () => data && render());
$("leftshow").onclick = () => {
  $("show").value = "leftover";
  render();
};

// ------------------------------------------------------------------ selection + bulk actions
$("selall").onchange = (e) => {
  for (const t of visible()) e.target.checked ? selected.add(t.hash) : selected.delete(t.hash);
  render();
};
function bulkBar() {
  const n = selected.size;
  $("bulk").hidden = !n || tab !== "t";
  if (!n) return;
  const picked = data.torrents.filter((t) => selected.has(t.hash));
  setText($("bulkn"), `${n} selected · ${size(picked.reduce((a, t) => a + t.size, 0))}`);
  $("b-undo").hidden = !picked.some((t) => t.undo === "ok");
  $("b-queue").hidden = !picked.some((t) => t.leftover);
}
const pickedHashes = () => [...selected];
$("b-clear").onclick = () => {
  selected.clear();
  render();
};
$("b-rule").onclick = () => editRule(pickedHashes());
$("b-protect").onclick = async () => {
  const hs = pickedHashes();
  try {
    await postJSON("/api/rule", { hashes: hs, mode: "never" });
    toast(`${hs.length} torrent${hs.length === 1 ? "" : "s"} protected`);
    selected.clear();
    load();
  } catch (e) {
    toast(e.message, true);
  }
};
$("b-queue").onclick = async () => {
  const hs = data.torrents.filter((t) => selected.has(t.hash) && t.leftover).map((t) => t.hash);
  try {
    await postJSON("/api/queue", { hashes: hs });
    toast(`${hs.length} leftover${hs.length === 1 ? "" : "s"} added to the cleanup list`);
    selected.clear();
    load();
  } catch (e) {
    toast(e.message, true);
  }
};
$("b-undo").onclick = () => askUndo(data.torrents.filter((t) => selected.has(t.hash) && t.undo === "ok").map((t) => t.hash));
$("b-del").onclick = () => askDelete(pickedHashes());

// ------------------------------------------------------------------ default rule + check now
function sentence() {
  const f = new FormData($("rules")),
    m = f.get("mode"),
    d = f.get("min_seed_days"),
    r = f.get("min_ratio");
  if (m === "never")
    return ($("sentence").innerHTML =
      "Torrents are <b>never</b> deleted automatically, unless a seeding rule in Settings says otherwise.");
  const ds = d !== "" && m !== "ratio" ? `has seeded <b>${+d} day${+d === 1 ? "" : "s"}</b>` : "";
  const rs = r !== "" && m !== "days" ? `reached <b>ratio ${+r}</b>` : "";
  const cond =
    ds && rs
      ? m === "both"
        ? `${ds} <b>and</b> ${rs}`
        : `${ds} <b>or</b> ${rs}, whichever comes first`
      : ds || rs || "<b>straight away</b> (no seeding rule)";
  $("sentence").innerHTML =
    `After you delete something in Jellyfin, its torrent is removed once it ${ds || rs ? "" : "is deleted "}${cond}.` +
    (f.get("dry_run") ? " <b>Dry run:</b> only logged for now." : "");
}
$("rules").addEventListener("input", sentence);
sentence();
$("rules").onsubmit = async (e) => {
  e.preventDefault();
  $("rsave").disabled = true;
  try {
    await post("/rules", new URLSearchParams(new FormData($("rules"))), "application/x-www-form-urlencoded");
    await load();
    toast("Default rule saved");
  } catch (err) {
    toast("Save failed: " + err.message, true);
  } finally {
    $("rsave").disabled = false;
  }
};
$("checknow").onclick = async () => {
  const b = $("checknow");
  b.disabled = true;
  b.firstChild.classList.add("spin");
  b.lastChild.textContent = "Checking…";
  try {
    await post("/sweep");
    await load();
    toast("Checked waiting torrents");
  } catch (e) {
    toast("Check failed: " + e.message, true);
  } finally {
    b.disabled = false;
    b.firstChild.classList.remove("spin");
    b.lastChild.textContent = "Check now";
  }
};

// ------------------------------------------------------------------ per-torrent rule dialog (one or many)
const dmode = $("dmode");
for (const [k, v] of Object.entries(MODES)) dmode.add(new Option(v[0].toUpperCase() + v.slice(1), k));
let editing = [];
function syncInputs() {
  const m = dmode.value;
  $("ddays").disabled = !m || m === "ratio" || m === "never";
  $("dratio").disabled = !m || m === "days" || m === "never";
}
dmode.onchange = () => {
  if (dmode.value && !$("ddays").value && !$("dratio").value) {
    $("ddays").value = data.default.min_seed_days ?? "";
    $("dratio").value = data.default.min_ratio ?? "";
  }
  syncInputs();
};
function editRule(hashes) {
  editing = hashes;
  const t = hashes.length === 1 ? data.torrents.find((x) => x.hash === hashes[0]) : null;
  $("dname").textContent = t ? t.name : `${hashes.length} torrents`;
  dmode.value = t && t.custom ? t.rule.mode : "";
  $("ddays").value = t && t.custom ? (t.rule.min_seed_days ?? "") : "";
  $("dratio").value = t && t.custom ? (t.rule.min_ratio ?? "") : "";
  syncInputs();
  $("dlg").showModal();
}
$("dlg").addEventListener("close", async () => {
  if ($("dlg").returnValue !== "save") return;
  try {
    await postJSON("/api/rule", {
      hashes: editing,
      mode: dmode.value,
      min_seed_days: $("ddays").value === "" ? null : +$("ddays").value,
      min_ratio: $("dratio").value === "" ? null : +$("dratio").value,
    });
    if (editing.length > 1) selected.clear();
    await load();
    toast(dmode.value ? "Rule saved" : "Back to the default rule");
  } catch (e) {
    toast("Save failed: " + e.message, true);
  }
});

// ------------------------------------------------------------------ delete now (one or many)
const sysOf = (s) =>
  /Jellyfin/.test(s)
    ? ["jellyfin", "Jellyfin"]
    : /Sonarr/.test(s)
      ? ["sonarr", "Sonarr"]
      : /Radarr/.test(s)
        ? ["radarr", "Radarr"]
        : /Seerr/.test(s)
          ? ["seerr", "Seerr"]
          : /qBittorrent/.test(s)
            ? ["qbit", "qBittorrent"]
            : ["", "Step"];
let deleting = [];
async function askDelete(hashes) {
  deleting = hashes;
  const list = $("delsteps");
  $("delgo").disabled = true;
  $("deldry").hidden = !data.dry_run;
  if (hashes.length > 1) {
    const ts = data.torrents.filter((t) => hashes.includes(t.hash));
    $("delname").textContent = `${ts.length} torrents · ${size(ts.reduce((a, t) => a + t.size, 0))}`;
    list.textContent = "";
    for (const t of ts) {
      const li = document.createElement("li");
      li.innerHTML = '<span class="sys qbit">Torrent</span><span></span>';
      li.lastChild.textContent = (t.title ? t.title + " · " : "") + t.name;
      list.append(li);
    }
    const li = document.createElement("li");
    li.className = "m";
    li.textContent = "Each one is also removed from Jellyfin and Sonarr/Radarr, the same as Delete now on a single torrent.";
    list.append(li);
    $("delgo").disabled = false;
    return $("del").showModal();
  }
  const t = data.torrents.find((x) => x.hash === hashes[0]);
  $("delname").textContent = t.name;
  list.innerHTML = '<li class="m">Working out what will be removed…</li>';
  $("del").showModal();
  try {
    const r = await fetch("/api/plan?hash=" + hashes[0]);
    if (!r.ok) throw new Error(await r.text());
    const p = await r.json();
    list.textContent = "";
    for (const s of p.steps) {
      const li = document.createElement("li"),
        tag = document.createElement("span"),
        tx = document.createElement("span"),
        [c, l] = sysOf(s);
      tag.className = "sys " + c;
      tag.textContent = l;
      tx.textContent = s;
      li.append(tag, tx);
      list.append(li);
    }
    if (!p.steps.length) list.innerHTML = '<li class="m">Nothing to delete.</li>';
    else $("delgo").disabled = false;
  } catch (e) {
    list.textContent = "";
    const li = document.createElement("li");
    li.className = "badc";
    li.textContent = e.message;
    list.append(li);
  }
}
$("del").addEventListener("close", async () => {
  if ($("del").returnValue !== "go") return;
  try {
    await postJSON("/api/delete-now", { hashes: deleting });
    selected.clear();
    toast("Deleting… follow along in Activity");
    setTab("a");
  } catch (e) {
    toast("Delete failed: " + e.message, true);
  }
});

// ------------------------------------------------------------------ undo (one or many)
let undoing = [];
async function askUndo(hashes) {
  undoing = hashes;
  const list = $("undsteps");
  $("undgo").disabled = true;
  if (hashes.length > 1) {
    $("undname").textContent = `${hashes.length} torrents`;
    $("undintro").textContent = "Undo puts each of these back as if you never deleted it:";
    list.textContent = "";
    for (const t of data.torrents.filter((x) => hashes.includes(x.hash))) {
      const li = document.createElement("li");
      li.innerHTML = icon("check");
      li.append(t.queued_as || t.name);
      list.append(li);
    }
    $("undgo").disabled = false;
    return $("und").showModal();
  }
  const t = data.torrents.find((x) => x.hash === hashes[0]);
  $("undname").textContent = t.queued_as || t.name;
  $("undintro").textContent = "Undo puts it back as if you never deleted it:";
  list.innerHTML = '<li class="m">Checking what can be restored…</li>';
  $("und").showModal();
  try {
    const r = await fetch("/api/undo-plan?hash=" + hashes[0]);
    if (!r.ok) throw new Error(await r.text());
    const p = await r.json();
    list.textContent = "";
    if (p.problem) {
      const li = document.createElement("li");
      li.className = "problem";
      li.innerHTML = icon("alert");
      li.append("Can't undo: " + p.problem + ".");
      return list.append(li);
    }
    for (const s of p.steps) {
      const li = document.createElement("li");
      li.innerHTML = icon("check");
      li.append(s);
      list.append(li);
    }
    $("undgo").disabled = false;
  } catch (e) {
    list.textContent = "";
    const li = document.createElement("li");
    li.className = "problem";
    li.textContent = e.message;
    list.append(li);
  }
}
$("und").addEventListener("close", async () => {
  if ($("und").returnValue !== "go") return;
  try {
    await postJSON("/api/undo", { hashes: undoing });
    selected.clear();
    toast("Restoring… follow along in Activity");
    setTab("a");
  } catch (e) {
    toast("Undo failed: " + e.message, true);
  }
});

// ------------------------------------------------------------------ tabs: torrents / activity / history
let tab = { "#activity": "a", "#history": "h" }[location.hash] || "t";
function setTab(x) {
  tab = x;
  for (const k of "tah") {
    $("pane-" + k).hidden = x !== k;
    $("tab-" + k).className = x === k ? "on" : "";
  }
  history.replaceState(null, "", { a: "#activity", h: "#history" }[x] || location.pathname);
  $("bulk").hidden = x !== "t" || !selected.size;
  refresh();
}
$("tab-t").onclick = () => setTab("t");
$("tab-a").onclick = () => setTab("a");
$("tab-h").onclick = () => setTab("h");

function lineClass(m) {
  if (/FAILED|error|failed/.test(m)) return "bad";
  if (/\[dry run\]/.test(m)) return "dry";
  if (/^\s*(done:|deleted|removed|unmonitored)/.test(m)) return "ok";
  return "";
}
function evIcon(head, cls) {
  if (cls === "bad") return "alert";
  if (cls === "dry") return "clock";
  if (/^(Jellyfin deleted|Delete now)/.test(head)) return "trash";
  if (/^deleted torrent|^Low disk/.test(head)) return "check";
  if (/started/.test(head)) return "power";
  if (/rule/i.test(head)) return "sliders";
  if (/Connection/.test(head)) return "plug";
  return cls === "ok" ? "check" : "info";
}
let logSig = "",
  logGroups = [];
function renderLog() {
  $("logrows").replaceChildren(
    ...logGroups.map((g) => {
      const [ts, head] = g[0],
        subs = g.slice(1),
        cls = [head, ...subs.map((x) => x[1])].map(lineClass);
      const k = cls.includes("bad")
        ? "bad"
        : cls.includes("dry")
          ? "dry"
          : cls.includes("ok")
            ? "ok"
            : /started|rule/i.test(head)
              ? "info"
              : "";
      const li = document.createElement("li");
      li.className = "ev " + k;
      li.innerHTML = `<div class="ic">${icon(evIcon(head.trim(), k))}</div><div><div class="hd"><b></b><time></time></div>${subs.length ? "<ul></ul>" : ""}</div>`;
      li.querySelector("b").textContent = head.trim();
      const tm = li.querySelector("time");
      tm.textContent = ago(ts);
      tm.title = new Date(ts * 1000).toLocaleString();
      tm.dataset.ts = ts;
      const ul = li.querySelector("ul");
      for (const [, m] of subs) {
        const s = document.createElement("li");
        s.className = lineClass(m);
        s.textContent = m.trim();
        ul.append(s);
      }
      return li;
    }),
  );
}
async function loadLog() {
  try {
    const r = await fetch("/api/log");
    if (!r.ok) throw new Error(r.status);
    const entries = await r.json();
    const sig = entries.length + ":" + (entries[0] || [0])[0];
    if (sig !== logSig) {
      logSig = sig;
      // entries arrive newest first; keep each event's indented detail lines under it, newest event on top
      const groups = [];
      for (const e of [...entries].reverse()) {
        if (e[1].startsWith(" ") && groups.length) groups[groups.length - 1].push(e);
        else groups.push([e]);
      }
      logGroups = groups.reverse();
      renderLog();
    } else for (const tm of $("logrows").querySelectorAll("time")) setText(tm, ago(+tm.dataset.ts));
    updState("lupd", "Live · updated " + clock());
  } catch (e) {
    updState("lupd", "Update failed: " + e.message, true);
  }
}

let histSig = "";
async function loadHistory() {
  try {
    const r = await fetch("/api/history?q=" + encodeURIComponent($("hq").value));
    if (!r.ok) throw new Error(r.status);
    const h = await r.json();
    const sig = JSON.stringify(h).length + ":" + h.rows.length + $("hq").value;
    setText($("hsum"), `${h.rows.length} cleaned up · ${size(h.total)} freed`);
    $("hempty").hidden = h.rows.length > 0;
    if (sig === histSig) return;
    histSig = sig;
    $("hrows").replaceChildren(
      ...h.rows.map((x) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          '<td class="num" data-l="When"></td><td class="n"><div class="t"></div><div class="meta"></div></td><td class="num" data-l="Size"></td><td data-l="Why"><span class="badge"></span><small class="redl"></small></td><td class="act"><button class="undo rdl" title="Download it again: puts it back in Sonarr/Radarr and re-adds the torrent or searches for a release">' +
          icon("refresh") +
          "<span>Redownload</span></button></td>";
        tr.children[0].textContent = new Date(x.ts * 1000).toLocaleString([], {
          month: "short",
          day: "numeric",
          year: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
        tr.children[1].firstChild.textContent = x.title || x.name;
        tr.children[1].lastChild.textContent = x.title && x.title !== x.name ? x.name : "";
        tr.children[2].textContent = size(x.size || 0);
        tr.children[3].firstChild.textContent = x.reason || "cleaned up";
        tr.children[3].firstChild.className =
          "badge " + (x.reason === "low disk space" ? "warn" : x.reason === "Delete now" ? "" : "ok");
        tr.children[3].lastChild.textContent = x.redownloaded
          ? `Redownload ${x.redownload_how === "same" ? "(same release)" : "(search)"} ${ago(x.redownloaded)}`
          : "";
        tr.children[4].firstChild.onclick = () => askRedownload(x);
        return tr;
      }),
    );
  } catch (e) {
    setText($("hsum"), "Couldn't load history: " + e.message);
  }
}
// ------------------------------------------------------------------ redownload (History)
let redl = null;
async function askRedownload(x) {
  redl = x;
  const list = $("rdlsteps");
  $("rdlname").textContent = x.title && x.title !== x.name ? `${x.title} · ${x.name}` : x.name;
  $("rdlgo").disabled = true;
  $("rdlhow").hidden = true;
  list.innerHTML = '<li class="m">Working out what this was…</li>';
  $("rdl").showModal();
  try {
    const r = await fetch("/api/redownload-plan?id=" + x.id);
    const p = await r.json();
    if (p.problem) {
      list.textContent = "";
      const li = document.createElement("li");
      li.className = "problem";
      li.innerHTML = icon("alert");
      li.append("Can't redownload: " + p.problem + ".");
      return list.append(li);
    }
    redl.plan = p;
    $("rdlwhat").textContent = p.what;
    $("rdlhow").hidden = false;
    $("rdl-same").disabled = !p.same_available;
    $("rdl-same-note").textContent = p.same_available
      ? "Exactly what you had. Good for private trackers."
      : "Not available: Shredarr didn't keep this torrent's file.";
    const how = p.same_available ? "same" : "search";
    document.querySelector(`input[name="rdlhow"][value="${how}"]`).checked = true;
    showRedlSteps();
    $("rdlgo").disabled = false;
  } catch (e) {
    list.textContent = "";
    const li = document.createElement("li");
    li.className = "problem";
    li.textContent = e.message;
    list.append(li);
  }
}
function showRedlSteps() {
  const how = document.querySelector('input[name="rdlhow"]:checked').value,
    list = $("rdlsteps");
  list.textContent = "";
  for (const s of redl.plan.steps[how]) {
    const li = document.createElement("li");
    li.innerHTML = icon("check");
    li.append(s);
    list.append(li);
  }
}
for (const el of document.querySelectorAll('input[name="rdlhow"]')) el.onchange = showRedlSteps;
$("rdl").addEventListener("close", async () => {
  if ($("rdl").returnValue !== "go") return;
  try {
    await postJSON("/api/redownload", { id: redl.id, how: document.querySelector('input[name="rdlhow"]:checked').value });
    toast("Redownload started. Follow along in Activity");
    setTab("a");
  } catch (e) {
    toast("Redownload failed: " + e.message, true);
  }
});

let hqTimer;
$("hq").oninput = () => {
  clearTimeout(hqTimer);
  hqTimer = setTimeout(loadHistory, 250);
};

const refresh = () => (ON_SETTINGS ? null : tab === "a" ? loadLog() : tab === "h" ? loadHistory() : load());

// ------------------------------------------------------------------ settings page
let settings = null;
function fill(form, obj) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in obj)) continue;
    if (el.type === "checkbox") el.checked = !!obj[el.name];
    else el.value = obj[el.name] ?? "";
  }
}
function formObj(form) {
  const o = {};
  for (const el of form.elements) if (el.name) o[el.name] = el.type === "checkbox" ? el.checked : el.value;
  return o;
}
function polText(p) {
  const parts = [`at least ${p.min_length} character${p.min_length > 1 ? "s" : ""}`];
  if (p.upper) parts.push("an uppercase letter");
  if (p.digit) parts.push("a number");
  if (p.symbol) parts.push("a symbol");
  return p.min_length <= 1 && !p.upper && !p.digit && !p.symbol ? "No requirements." : "Needs " + parts.join(", ") + ".";
}

function renderConnections() {
  for (const box of document.querySelectorAll(".conn")) {
    const svc = box.dataset.svc,
      conf = settings.connections[svc] || {},
      h = settings.health[svc];
    for (const el of box.querySelectorAll("input")) {
      const key = el.name.split(".")[1];
      el.value = conf[key] || "";
      if (el.type === "password") el.placeholder = conf.has_secret ? "saved, leave blank to keep" : "";
    }
    const dot = box.querySelector(".dot"),
      st = box.querySelector(".cstate");
    dot.className = "dot " + (!conf.url ? "" : h ? (h.ok ? "ok" : "bad") : "");
    st.textContent = !conf.url ? (svc === "seerr" ? "" : "not set up") : h ? h.msg : "checking…";
    st.className = "cstate" + (h && !h.ok ? " badc" : "");
  }
  $("connbox").classList.toggle("needs", settings.missing.length > 0);
  if (!$("f-wu").value) $("f-wu").value = settings.webhook_url_guess;
}

const RULE_TYPES = { category: "Category", title: "Show or movie", tracker: "Tracker" };
function ruleRow(r = { type: "category", match: "", mode: "either", min_seed_days: null, min_ratio: null }) {
  const div = document.createElement("div");
  div.className = "rrow";
  div.innerHTML =
    `<select class="rt" aria-label="Match by">${Object.entries(RULE_TYPES)
      .map(([k, v]) => `<option value="${k}">${v}</option>`)
      .join("")}</select>` +
    '<input class="rm" aria-label="Matches">' +
    `<select class="rmode" aria-label="Delete when">${Object.entries(MODES)
      .map(([k, v]) => `<option value="${k}">${v}</option>`)
      .join("")}</select>` +
    '<input class="rd" type="number" step="any" min="0" placeholder="days" aria-label="Minimum seed days"><input class="rr" type="number" step="any" min="0" placeholder="ratio" aria-label="Minimum ratio">' +
    `<button type="button" class="icon" title="Remove rule" aria-label="Remove rule">${icon("x")}</button>`;
  const [t, m, mode, d, ra, del] = div.children;
  t.value = r.type;
  m.value = r.match;
  mode.value = r.mode;
  d.value = r.min_seed_days ?? "";
  ra.value = r.min_ratio ?? "";
  const sync = () => {
    m.setAttribute("list", "dl-" + t.value);
    m.placeholder = { category: "e.g. tv-sonarr", title: "e.g. Last Week Tonight", tracker: "e.g. tracker.example.org" }[t.value];
    d.disabled = mode.value === "ratio" || mode.value === "never";
    ra.disabled = mode.value === "days" || mode.value === "never";
  };
  t.onchange = mode.onchange = sync;
  del.onclick = () => div.remove();
  sync();
  return div;
}
function mapRow(m = { remote: "", local: "" }) {
  const div = document.createElement("div");
  div.className = "mrow";
  div.innerHTML = `<input class="mr" placeholder="Path qBittorrent/Sonarr/Radarr use, e.g. /downloads/" aria-label="Their path"><span>→</span><input class="ml" placeholder="Same folder in Shredarr, e.g. /data/downloads/" aria-label="Shredarr path"><button type="button" class="icon" title="Remove" aria-label="Remove">${icon("x")}</button>`;
  div.querySelector(".mr").value = m.remote;
  div.querySelector(".ml").value = m.local;
  div.querySelector("button").onclick = () => div.remove();
  return div;
}
function fillList(id, items) {
  $(id).replaceChildren(...items.map((v) => Object.assign(document.createElement("option"), { value: v })));
}

async function loadSettings() {
  try {
    const r = await fetch("/api/settings");
    settings = await r.json();
    renderConnections();
    $("rlist").replaceChildren(...settings.rule_list.map(ruleRow));
    fillList("dl-category", settings.choices.categories);
    fillList("dl-title", settings.choices.titles);
    fillList("dl-tracker", settings.choices.trackers);
    $("maps").replaceChildren(...settings.path_maps.map(mapRow));
    fill($("nform"), settings.notify);
    fill($("gform"), settings.grace);
    gHelp();
    fill($("bform"), settings.behavior);
    fill($("sform"), settings.low_space);
    $("free-now").textContent =
      settings.free_gb != null ? `The downloads disk has ${settings.free_gb.toFixed(0)} GB free right now.` : "";
    fill($("rform"), settings.restore);
    fill($("polform"), settings.policy);
    $("uform").elements.enabled.checked = settings.update_check;
    const u = settings.update || {};
    $("about-ver").innerHTML =
      `Version <b>${settings.version}</b>` +
      (u.newer
        ? ` · <a href="${u.url}" target="_blank" rel="noopener">v${u.latest} is available</a>`
        : u.latest
          ? " · up to date"
          : "") +
      ` · <a href="https://github.com/${settings.repo}" target="_blank" rel="noopener">GitHub</a>`;
    $("pol-text").textContent = polText(settings.policy);
    $("rchecks").hidden = true;
    if (settings.missing.length) toast("Connect " + settings.missing.map((k) => NAMES[k]).join(", ") + " to get started");
  } catch (e) {
    toast("Could not load settings: " + e.message, true);
  }
}

function connValues(svc) {
  const conf = {};
  for (const el of document.querySelectorAll(`.conn[data-svc="${svc}"] input`)) conf[el.name.split(".")[1]] = el.value.trim();
  return conf;
}
for (const b of document.querySelectorAll(".ctest"))
  b.onclick = async () => {
    const box = b.closest(".conn"),
      svc = box.dataset.svc,
      st = box.querySelector(".cstate"),
      dot = box.querySelector(".dot");
    b.disabled = true;
    st.textContent = "testing…";
    try {
      const r = JSON.parse(await postJSON("/api/test-connection", { service: svc, conf: connValues(svc) }));
      dot.className = "dot " + (r.ok ? "ok" : "bad");
      st.textContent = r.msg;
      st.className = "cstate" + (r.ok ? "" : " badc");
      toast(r.ok ? `${NAMES[svc]} works: ${r.msg}` : `${NAMES[svc]}: ${r.msg}`, !r.ok);
    } catch (e) {
      toast(e.message, true);
    } finally {
      b.disabled = false;
    }
  };
$("cform").onsubmit = async (e) => {
  e.preventDefault();
  const all = {};
  for (const svc of Object.keys(NAMES)) all[svc] = connValues(svc);
  try {
    await postJSON("/api/connections", all);
    toast("Connections saved. Checking them now…");
    setTimeout(loadSettings, 2500);
  } catch (err) {
    toast(err.message, true);
  }
};
$("wform").onsubmit = async (e) => {
  e.preventDefault();
  try {
    toast(await postJSON("/api/jellyfin-webhook", formObj($("wform"))));
  } catch (err) {
    toast(err.message, true);
  }
};
$("addmap").onclick = () => $("maps").append(mapRow());
$("mform").onsubmit = async (e) => {
  e.preventDefault();
  const maps = [...document.querySelectorAll(".mrow")].map((d) => ({
    remote: d.querySelector(".mr").value,
    local: d.querySelector(".ml").value,
  }));
  try {
    await postJSON("/api/path-maps", { maps });
    toast("Path mappings saved");
    loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
};
$("addrule").onclick = () => $("rlist").append(ruleRow());
$("lform").onsubmit = async (e) => {
  e.preventDefault();
  const rules = [...document.querySelectorAll(".rrow")].map((d) => ({
    type: d.querySelector(".rt").value,
    match: d.querySelector(".rm").value,
    mode: d.querySelector(".rmode").value,
    min_seed_days: d.querySelector(".rd").value === "" ? null : +d.querySelector(".rd").value,
    min_ratio: d.querySelector(".rr").value === "" ? null : +d.querySelector(".rr").value,
  }));
  try {
    await postJSON("/api/rule-list", { rules });
    toast("Seeding rules saved");
    loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
};
for (const el of $("bform").elements)
  el.onchange = async () => {
    try {
      await postJSON("/api/behavior", formObj($("bform")));
      toast("Cleanup behavior saved");
    } catch (err) {
      toast(err.message, true);
    }
  };
$("bform").onsubmit = (e) => e.preventDefault();
$("sform").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await postJSON("/api/low-space", formObj($("sform")));
    toast("Low disk space setting saved");
  } catch (err) {
    toast(err.message, true);
  }
};
$("uform").elements.enabled.onchange = async (e) => {
  try {
    await postJSON("/api/update-check", { enabled: e.target.checked });
    toast(e.target.checked ? "Update checks on" : "Update checks off");
  } catch (err) {
    toast(err.message, true);
  }
};
$("uform").onsubmit = (e) => e.preventDefault();

async function saveNotify(quiet) {
  await postJSON("/api/settings", { notify: formObj($("nform")) });
  if (!quiet) toast("Notification settings saved");
}
$("nform").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await saveNotify();
  } catch (err) {
    toast(err.message, true);
  }
};
$("ntest").onclick = async () => {
  const b = $("ntest");
  b.disabled = true;
  try {
    await saveNotify(true);
    toast(await postJSON("/api/notify-test", {}));
  } catch (e) {
    toast("Test failed: " + e.message, true);
  } finally {
    b.disabled = false;
  }
};
$("rform").elements.enabled.onchange = async (e) => {
  try {
    await postJSON("/api/restore", { enabled: e.target.checked });
    toast(e.target.checked ? "RAR restore turned on" : "RAR restore turned off");
  } catch (err) {
    toast(err.message, true);
  }
};
$("rform").onsubmit = (e) => e.preventDefault();
function gHelp() {
  const f = formObj($("gform")),
    v = +f.value || 0;
  $("g-help").textContent = v
    ? `Torrents wait at least ${v} ${v === 1 ? f.unit.slice(0, -1) : f.unit} after you delete in Jellyfin. Sonarr/Radarr cleanup still happens right away; Undo reverses it.`
    : "0 means no delay: the torrent is removed as soon as its seeding rule is met.";
}
$("gform").oninput = gHelp;
$("gform").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await postJSON("/api/grace", formObj($("gform")));
    toast("Deletion delay saved");
  } catch (err) {
    toast(err.message, true);
  }
};
$("rcheck").onclick = async () => {
  const b = $("rcheck");
  b.disabled = true;
  const r = await fetch("/api/restore-check", { method: "POST", headers: { "X-Fetch": "1" } });
  const d = await r.json(),
    ul = $("rchecks");
  ul.textContent = "";
  d.messages.forEach((m, i) => {
    const li = document.createElement("li");
    li.className = !d.ok && i === d.messages.length - 1 ? "bad" : "ok";
    li.textContent = (li.className === "ok" ? "✓ " : "✕ ") + m;
    ul.append(li);
  });
  ul.hidden = false;
  b.disabled = false;
};
$("polform").onsubmit = async (e) => {
  e.preventDefault();
  const o = formObj($("polform"));
  try {
    await postJSON("/api/password-policy", o);
    settings.policy = { ...o, min_length: Math.max(1, +o.min_length || 1) };
    $("pol-text").textContent = polText(settings.policy);
    toast("Password rules saved");
  } catch (err) {
    toast(err.message, true);
  }
};
$("polnone").onclick = () => {
  fill($("polform"), { min_length: 1, upper: false, digit: false, symbol: false });
  $("polform").requestSubmit();
};
$("pform").onsubmit = async (e) => {
  e.preventDefault();
  const f = $("pform").elements;
  if (f.new.value !== f.confirm.value) return toast("The new passwords don't match", true);
  const r = await fetch("/api/password", {
    method: "POST",
    headers: { "X-Fetch": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ current: f.current.value, new: f.new.value }),
  });
  if (r.ok) {
    $("pform").reset();
    toast("Password changed");
  } else toast(await r.text(), true);
};
$("hlogout").onclick = async () => {
  await _fetch("/logout", { method: "POST", headers: { "X-Fetch": "1" } });
  location.href = "/login";
};
$("logoutall").onclick = async () => {
  if (!confirm("Sign out on every device, including this one?")) return;
  await _fetch("/api/logout-all", { method: "POST", headers: { "X-Fetch": "1" } });
  location.href = "/login";
};

// On phones, each settings box and service group folds open/closed; which ones are open is remembered.
function collapsibles() {
  const key = "shredarr-open",
    phone = matchMedia("(max-width:760px)");
  let open = {};
  try {
    open = JSON.parse(localStorage.getItem(key)) || {};
  } catch (e) {}
  for (const h of document.querySelectorAll("#spage .sbox>h3, #spage .svc>.svc-h")) {
    const box = h.parentElement,
      id = h.textContent.trim();
    box.classList.toggle("collapsed", !open[id]);
    h.setAttribute("role", "button");
    h.tabIndex = 0;
    h.setAttribute("aria-expanded", !!open[id]);
    const flip = () => {
      if (!phone.matches) return;
      box.classList.toggle("collapsed");
      open[id] = !box.classList.contains("collapsed");
      h.setAttribute("aria-expanded", open[id]);
      try {
        localStorage.setItem(key, JSON.stringify(open));
      } catch (e) {}
    };
    h.onclick = flip;
    h.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        flip();
      }
    };
  }
}

// ------------------------------------------------------------------ start
if (ON_SETTINGS) {
  collapsibles();
  document.body.classList.add("on-settings");
  $("spage").hidden = false;
  $("hset").hidden = true;
  $("hback").hidden = false;
  document.title = "Settings · Shredarr";
  loadSettings();
} else {
  setTab(tab);
  if (tab !== "t") load();
}
setInterval(() => {
  if (!document.hidden && !$("dlg").open && !$("del").open && !$("und").open && !$("rdl").open) refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
