"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const REPLAY_SPEED = 8;
  const REPLAY_MAX_GAP_MS = 1200;
  const VOICE_STAGE_MS = 8000;
  const VOICE_FIELDS = ["client", "scope", "value_range", "dates", "role", "outcome", "reference"];

  const state = { firms: [], sols: [], firmId: null, solId: null, running: false, playToken: 0, run: null, voice: null, audio: null };

  // ---------- helpers ----------
  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  function store(op, key, val) {
    try {
      if (op === "get") return localStorage.getItem(key);
      localStorage.setItem(key, val);
    } catch (e) { /* storage unavailable: the console still works, it just forgets */ }
    return null;
  }
  function session(op, key, val) {
    try {
      if (op === "get") return sessionStorage.getItem(key);
      if (op === "del") sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, val);
    } catch (e) { /* ignore */ }
    return null;
  }
  async function errorText(res) {
    const body = await res.text();
    try {
      const d = JSON.parse(body).detail;
      if (typeof d === "string") return `${res.status}: ${d}`;
      if (Array.isArray(d)) return `${res.status}: ` + d.map((x) => x.msg || JSON.stringify(x)).join("; ");
    } catch (e) { /* not JSON */ }
    return `${res.status}: ${body.slice(0, 600) || res.statusText}`;
  }
  async function api(path, opts) {
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw new Error(`network error: ${e.message}`); }
    if (!res.ok) { const err = new Error(await errorText(res)); err.status = res.status; throw err; }
    return res;
  }
  function showErr(id, msg) { const n = $(id); n.textContent = msg || ""; n.hidden = !msg; }
  const fmtTime = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false }); };
  const firm = () => state.firms.find((f) => f.id === state.firmId);
  const sol = () => state.sols.find((s) => s.id === state.solId);

  // ---------- firms ----------
  async function loadFirms(keep) {
    try {
      state.firms = await (await api("/firms")).json();
      showErr("firm-error", "");
    } catch (e) { showErr("firm-error", `Could not load firms. ${e.message}`); return; }
    const sel = $("firm-select");
    sel.replaceChildren(...state.firms.map((f) => h("option", { value: f.id }, f.name)));
    if (!keep || !firm()) state.firmId = state.firms.length ? state.firms[0].id : null;
    sel.value = state.firmId;
    renderFirm();
  }
  function renderFirm() {
    const f = firm();
    if (!f) return;
    $("firm-card").replaceChildren(
      h("div", { class: "name" }, f.name, " ", f.hubzone ? h("span", { class: "badge hz" }, "HUBZONE") : null),
      h("div", { class: "small" }, "NAICS: ", (f.naics_codes || []).join(", ") || "none"),
      h("div", { class: "small muted" }, f.size_standard || ""),
    );
    $("pp-list").replaceChildren(...f.past_performance.map((e, i) => {
      const meta = [e.dates || e.period, e.contract_value || e.value_range].filter(Boolean).join(" · ");
      return h("li", {},
        h("div", { class: "client" }, e.client || "(no client)"),
        h("div", { class: "scope", title: e.scope || "" }, e.scope || ""),
        h("div", { class: "meta" },
          h("span", { class: "badge" + (e.verified ? " v" : "") }, e.verified ? "VERIFIED" : "UNVERIFIED"),
          meta ? h("span", {}, meta) : null,
          e.verified ? h("button", { class: "tiny", type: "button", onclick: () => removeEntry(i) }, "Remove") : null));
    }));
    $("replay-btn").disabled = !store("get", lastRunKey(f.id));
  }
  function adminKey() {
    let key = session("get", "rfp.adminKey");
    if (!key) {
      key = (window.prompt("Admin key (kept for this browser session):") || "").trim();
      if (key) session("set", "rfp.adminKey", key);
    }
    return key;
  }
  async function removeEntry(index) {
    const key = adminKey();
    if (!key) return;
    try {
      await api(`/firms/${state.firmId}/past-performance/${index}`, { method: "DELETE", headers: { "X-Admin-Key": key } });
      showErr("firm-error", "");
      await loadFirms(true);
    } catch (e) {
      if (e.status === 401) { session("del", "rfp.adminKey"); showErr("firm-error", "admin key rejected"); }
      else showErr("firm-error", `Remove failed. ${e.message}`);
    }
  }

  // ---------- solicitations ----------
  async function loadSols() {
    try {
      state.sols = await (await api("/solicitations")).json();
      showErr("sol-error", "");
    } catch (e) { showErr("sol-error", `Could not load solicitations. ${e.message}`); return; }
    if (!sol()) state.solId = state.sols.length ? state.sols[0].id : null;
    renderSols();
  }
  async function removeSol(id) {
    const key = adminKey();
    if (!key) return;
    try {
      await api(`/solicitations/${encodeURIComponent(id)}`, { method: "DELETE", headers: { "X-Admin-Key": key } });
      await loadSols();
    } catch (e) {
      if (e.status === 401) { session("del", "rfp.adminKey"); showErr("sol-error", "admin key rejected"); }
      else showErr("sol-error", `Remove failed. ${e.message}`);
    }
  }
  function renderSols() {
    $("sol-list").replaceChildren(...state.sols.map((s) => h("li", { class: s.id === state.solId ? "sel" : "", onclick: () => { state.solId = s.id; renderSols(); } },
      h("div", { class: "t" }, s.title || s.id, " ", s.demo ? h("span", { class: "badge" }, "DEMO") : h("button", { class: "tiny", type: "button", onclick: (ev) => { ev.stopPropagation(); removeSol(s.id); } }, "Remove")),
      h("div", { class: "m" }, [s.naics && `NAICS ${s.naics}`, s.set_aside, s.due_date && `due ${s.due_date}`].filter(Boolean).join(" · ")),
      s.participation_goals ? h("div", { class: "m" }, `Goals: ${s.participation_goals}`) : null)));
  }

  // ---------- trace ----------
  const lastRunKey = (firmId) => `rfp.lastRun.${firmId}`;
  function resetRun() {
    state.run = { id: null, events: [], rows: [], narrative: null, capability: null, decision: null, complete: null };
    $("trace").replaceChildren();
    $("verdict").replaceChildren();
    $("results").hidden = true;
    $("run-footer").textContent = "";
    showErr("run-error", "");
    updateDownloads();
  }
  function moveCursor() {
    document.querySelectorAll("#trace .cursor").forEach((c) => c.remove());
    const last = $("trace").lastElementChild;
    if (last) last.querySelector(".detail").append(h("span", { class: "cursor" }));
  }
  function clearCursor() { document.querySelectorAll("#trace .cursor").forEach((c) => c.remove()); }
  const json = (v) => JSON.stringify(v, null, 1);

  function describe(ev) {
    switch (ev.event) {
      case "run_started": return ["run_started", "", `run ${String(ev.run_id).slice(0, 8)}`];
      case "step_started": return ["step", "", ev.step];
      case "tool_call": return ["tool_call", "call", [h("b", {}, ev.tool), " ", h("span", { class: "muted" }, JSON.stringify(ev.args))]];
      case "tool_result": return ["tool_result", "", [h("b", {}, ev.tool), h("details", {}, h("summary", {}, "result"), h("pre", {}, ev.summary))]];
      case "decision": return ["decision", "call", `${ev.payload.verdict} (score ${ev.payload.score})`];
      case "matrix_row": return ["matrix_row", "", `${ev.row.gap === "full" ? "GAP" : ev.row.gap === "partial" ? "partial" : "met"} · ${ev.row.requirement}`];
      case "narrative": return ["narrative", "", `${ev.text.split(/\s+/).length} words written`];
      case "capability_statement": return ["capability", "", ev.payload.headline];
      case "step_failed": return ["step_failed", "bad", `${ev.step}: ${ev.reason}`];
      case "run_complete": return ["run_complete", "call", `verdict ${ev.verdict}${ev.readiness_pct != null ? ` · readiness ${ev.readiness_pct}%` : ""}`];
      default: return [ev.event, "", h("details", {}, h("summary", {}, "data"), h("pre", {}, json(ev)))];
    }
  }
  function renderEvent(ev, fresh) {
    state.run.events.push(ev);
    const [label, cls, detail] = describe(ev);
    $("trace").append(h("li", {}, h("span", { class: "ts" }, fmtTime(ev.timestamp)), h("span", { class: "ev " + cls }, label), h("span", { class: "detail" }, detail)));
    $("trace").lastElementChild.scrollIntoView({ block: "nearest" });
    if (ev.event === "run_started") {
      state.run.id = ev.run_id;
      store("set", lastRunKey(ev.firm_id), ev.run_id);
      $("replay-btn").disabled = false;
    } else if (ev.event === "decision") {
      state.run.decision = ev.payload;
      renderVerdict(ev.payload, fresh);
    } else if (ev.event === "matrix_row") {
      state.run.rows.push(ev.row);
      renderResults();
    } else if (ev.event === "narrative") {
      state.run.narrative = ev.text;
      renderResults();
    } else if (ev.event === "capability_statement") {
      state.run.capability = ev.payload;
      renderResults();
    } else if (ev.event === "run_complete" || ev.event === "step_failed") {
      state.run.complete = ev;
      if (ev.event === "step_failed") showErr("run-error", `Step ${ev.step} failed: ${ev.reason}`);
    }
    moveCursor();
  }

  function renderVerdict(d, fresh) {
    const entries = fresh && firm() ? firm().past_performance : [];
    const matches = (d.verified_matches || []).map((m) => h("li", {}, h("b", {}, entries[m.entry_index] ? entries[m.entry_index].client : `Entry #${m.entry_index}`), ": ", m.why));
    const list = (title, items) => items && items.length ? [h("h4", {}, title), h("ul", {}, items.map((x) => h("li", {}, x)))] : [];
    $("verdict").replaceChildren(h("div", { class: `verdict ${d.verdict}` },
      h("div", { class: "head" },
        h("span", { class: "v" }, d.verdict.replace("_", " ")),
        h("span", { class: "score" }, `${d.score}/100`),
        h("span", { class: "badge" }, `${d.verdict_source === "rule" ? "RULE" : "MODEL"} verdict`),
        h("span", { class: "muted small" }, `est. effort ${d.estimated_effort_hours} h`)),
      d.rule_reason ? h("div", { class: "small muted" }, d.rule_reason) : null,
      list("Top reasons", d.top_reasons),
      d.blockers && d.blockers.length ? [h("h4", {}, "Blockers"), h("ul", {}, d.blockers.map((b) => h("li", { class: "st full" }, b)))] : null,
      h("h4", {}, `Verified matches (${matches.length})`),
      matches.length ? h("ul", {}, matches) : h("div", { class: "small muted" }, "none")));
  }

  // ---------- results ----------
  const STATUS = { none: "Met", partial: "Partial", full: "Gap" };
  function renderResults() {
    const r = state.run;
    $("results").hidden = !(r.rows.length || r.narrative || r.capability);
    $("tab-matrix").replaceChildren(h("table", { class: "mx" },
      h("thead", {}, h("tr", {}, ["Requirement", "Type", "Status", "Evidence", "Draft response"].map((c) => h("th", {}, c)))),
      h("tbody", {}, r.rows.map((x) => h("tr", { class: x.gap === "full" ? "gap" : "" },
        h("td", {}, x.requirement), h("td", {}, x.requirement_type),
        h("td", {}, h("span", { class: `st ${x.gap}` }, STATUS[x.gap] || x.gap)),
        h("td", {}, x.firm_capability), h("td", {}, x.draft_response))))));
    $("tab-narrative").replaceChildren(r.narrative ? h("p", {}, r.narrative) : h("p", { class: "muted" }, "No narrative in this run."));
    const c = r.capability;
    $("tab-capability").replaceChildren(...(c ? [
      h("h4", {}, c.headline),
      h("p", { class: "muted" }, `${c.company_data.name} · ${c.company_data.location || ""} · NAICS ${c.company_data.naics_codes.join(", ")} · ${c.company_data.certifications.join(", ")}`),
      ...[["Core competencies", c.core_competencies], ["Differentiators", c.differentiators], ["Past performance", c.past_performance_summaries]]
        .flatMap(([t, items]) => [h("h4", {}, t), h("ul", {}, items.map((i) => h("li", {}, i)))]),
    ] : [h("p", { class: "muted" }, "No capability statement in this run.")]));
    updateDownloads();
  }
  function updateDownloads() {
    const r = state.run || {};
    const have = { capability: !!r.capability, matrix: !!(r.rows && r.rows.length), bidpacket: !!(r.capability && r.rows && r.rows.length && r.narrative) };
    const sid = (r.events && r.events[0] && r.events[0].solicitation_id) || state.solId;
    const fid = (r.events && r.events[0] && r.events[0].firm_id) || state.firmId;
    for (const kind of ["capability", "matrix", "bidpacket"]) {
      const a = $(`dl-${kind}`);
      a.href = have[kind] ? `/agent/export/${encodeURIComponent(sid)}?firm_id=${fid}&kind=${kind}` : "#";
      a.classList.toggle("off", !have[kind]);
    }
  }
  function selectTab(name) {
    document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    for (const t of ["matrix", "narrative", "capability"]) $(`tab-${t}`).hidden = t !== name;
  }

  async function showFooter(usage, events) {
    const done = events.find((e) => e.event === "run_complete");
    const t = (e) => new Date(e.timestamp).getTime();
    const secs = events.length > 1 ? ((t(events[events.length - 1]) - t(events[0])) / 1000).toFixed(1) : "?";
    const n = (v) => Number(v || 0).toLocaleString();
    const cost = done && done.est_cost_usd != null ? `$${Number(done.est_cost_usd).toFixed(4)}` : "n/a";
    $("run-footer").textContent = `tokens in ${n(usage.tokens_in)} · out ${n(usage.tokens_out)} · cached ${n(usage.cached_tokens)} · latency ${secs}s · est. cost ${cost}`;
  }
  async function footerFor(runId, events) {
    try { showFooter((await (await api(`/runs/${runId}`)).json()).usage, events); }
    catch (e) { $("run-footer").textContent = `Usage unavailable. ${e.message}`; }
  }

  // ---------- live run (SSE over fetch) ----------
  function setBusy(busy, text) {
    state.running = busy;
    $("run-btn").disabled = busy;
    $("replay-btn").disabled = busy || !store("get", lastRunKey(state.firmId));
    $("run-status").textContent = text || "";
  }
  async function runAgent() {
    if (state.running || !state.firmId || !state.solId) return;
    state.playToken++;
    resetRun();
    setBusy(true, "Agent running...");
    try {
      const res = await api("/agent/run", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ firm_id: state.firmId, solicitation_id: state.solId }),
      });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.search(/\r?\n\r?\n/)) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut).replace(/^\r?\n\r?\n/, "");
          const data = frame.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).replace(/^ /, "")).join("\n");
          if (data) renderEvent(JSON.parse(data), true);
        }
      }
      if (!state.run.complete) showErr("run-error", "The stream ended before the run finished. Use Replay last run to see what was stored.");
    } catch (e) {
      showErr("run-error", `Run failed. ${e.message}`);
    }
    clearCursor();
    setBusy(false, state.run.complete ? "Done" : "");
    if (state.run.id) footerFor(state.run.id, state.run.events);
  }

  // ---------- replay ----------
  async function replay() {
    if (state.running || !state.firmId) return;
    const id = store("get", lastRunKey(state.firmId));
    if (!id) return;
    let run;
    try { run = await (await api(`/runs/${id}`)).json(); }
    catch (e) { showErr("run-error", `Replay failed. ${e.message}`); return; }
    const token = ++state.playToken;
    resetRun();
    state.firmId = run.firm_id;
    if (state.sols.some((s) => s.id === run.solicitation_id)) state.solId = run.solicitation_id;
    $("firm-select").value = state.firmId;
    renderFirm();
    renderSols();
    setBusy(true, `Replaying at ${REPLAY_SPEED}x...`);
    let prev = null;
    for (const ev of run.events) {
      const gap = prev ? Math.min((new Date(ev.timestamp) - new Date(prev.timestamp)) / REPLAY_SPEED, REPLAY_MAX_GAP_MS) : 0;
      if (gap > 0) await new Promise((r) => setTimeout(r, gap));
      if (token !== state.playToken) return;
      renderEvent(ev, false);
      prev = ev;
    }
    clearCursor();
    setBusy(false, "Replay done");
    showFooter(run.usage, run.events);
  }

  // ---------- voice ----------
  let recorder = null;
  function setAudio(blob, name) {
    state.audio = { blob, name };
    $("audio-name").textContent = name;
    const p = $("audio-preview");
    p.src = URL.createObjectURL(blob);
    p.hidden = false;
    $("voice-submit").disabled = false;
  }
  async function toggleRecord() {
    const btn = $("record-btn");
    if (recorder) { recorder.stop(); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { showErr("voice-error", "Recording is not supported in this browser. Use Upload."); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => chunks.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        recorder = null;
        btn.textContent = "Record";
        btn.classList.remove("rec");
        setAudio(new Blob(chunks, { type: "audio/webm" }), "recording.webm");
      };
      recorder.start();
      btn.textContent = "Stop";
      btn.classList.add("rec");
      showErr("voice-error", "");
    } catch (e) { showErr("voice-error", `Microphone unavailable. ${e.message}`); }
  }
  async function submitVoice() {
    if (!state.audio) return;
    showErr("voice-error", "");
    $("voice-result").hidden = true;
    $("voice-done").hidden = true;
    $("voice-submit").disabled = true;
    const prog = $("voice-progress");
    prog.hidden = false;
    prog.textContent = "Transcribing with Muse Voice Transcribe...";
    const timer = setTimeout(() => { prog.textContent = "Extracting with Muse Spark..."; }, VOICE_STAGE_MS);
    const fd = new FormData();
    fd.append("firm_id", state.firmId);
    fd.append("hints", $("hints").value.trim());
    fd.append("audio", state.audio.blob, state.audio.name);
    try {
      state.voice = await (await api("/voice/past-performance", { method: "POST", body: fd })).json();
      renderVoice();
    } catch (e) { showErr("voice-error", e.status === 502 ? `Voice service error. ${e.message}` : e.message); }
    clearTimeout(timer);
    prog.hidden = true;
    $("voice-submit").disabled = false;
  }
  function renderVoice() {
    const v = state.voice;
    $("voice-transcript").textContent = v.transcript;
    $("voice-fields").replaceChildren(...VOICE_FIELDS.map((f) => h("div", { class: "field" },
      h("label", { for: `vf-${f}` }, f.replace("_", " ")),
      h("input", { type: "text", id: `vf-${f}`, value: v.entry[f] == null ? "" : v.entry[f], placeholder: "not mentioned" }),
      v.entry[f] != null && v.evidence[f] ? h("div", { class: "q" }, `"${v.evidence[f]}"`) : h("div", { class: "q" }, v.entry[f] == null ? "not mentioned" : ""))));
    $("voice-result").hidden = false;
  }
  async function confirmVoice() {
    const v = state.voice;
    const edits = {};
    for (const f of VOICE_FIELDS) {
      const now = $(`vf-${f}`).value.trim();
      if (now !== (v.entry[f] == null ? "" : v.entry[f])) edits[f] = now || null;
    }
    try {
      await api("/voice/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pending_id: v.pending_id, edits }) });
      state.voice = null;
      $("voice-result").hidden = true;
      $("voice-done").textContent = "Saved as VERIFIED past performance.";
      $("voice-done").hidden = false;
      showErr("voice-error", "");
      await loadFirms(true);
    } catch (e) { showErr("voice-error", `Confirm failed. ${e.message}`); }
  }
  async function rejectVoice() {
    try {
      await api("/voice/reject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pending_id: state.voice.pending_id }) });
      state.voice = null;
      $("voice-result").hidden = true;
      showErr("voice-error", "");
    } catch (e) { showErr("voice-error", `Reject failed. ${e.message}`); }
  }

  // ---------- health ----------
  async function health() {
    try {
      const d = await (await api("/health")).json();
      $("health").textContent = `health ok · model ${d.model || "?"} · API ${d.api_base || "?"}`;
    } catch (e) { $("health").textContent = `health check failed. ${e.message}`; }
  }

  // ---------- wiring ----------
  $("firm-select").addEventListener("change", (e) => { state.firmId = Number(e.target.value); renderFirm(); });
  $("run-btn").addEventListener("click", runAgent);
  $("replay-btn").addEventListener("click", replay);
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("upload-btn").addEventListener("click", () => $("audio-file").click());
  $("audio-file").addEventListener("change", (e) => { const f = e.target.files[0]; if (f) setAudio(f, f.name); });
  $("record-btn").addEventListener("click", toggleRecord);
  $("voice-submit").addEventListener("click", submitVoice);
  $("voice-confirm").addEventListener("click", confirmVoice);
  $("voice-reject").addEventListener("click", rejectVoice);

  resetRun();
  loadFirms(false);
  loadSols();
  health();
})();
