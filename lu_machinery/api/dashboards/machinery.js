"use strict";

(function initMachinery(root) {
  const API_TIMEOUT_MS = 8000;
  const REFRESH_MS = 30000;
  const OPTIONAL_WAIT_MS = 1500;
  const RETRY_LIMIT = 10;
  const THEME_KEY = "machinery-theme";
  const THEMES = ["auto", "light", "dark"];

  // The page navigation is rendered by the server from the page contracts (main.py), so it is
  // in the HTML before this script runs.

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  function safeHttpUrl(value) {
    return typeof value === "string" && /^https?:\/\//i.test(value) ? value : null;
  }

  // ---- Navigation & theme ----

  function readStoredTheme() {
    try {
      const saved = typeof localStorage === "object" && localStorage ? localStorage.getItem(THEME_KEY) : null;
      return THEMES.includes(saved) ? saved : "auto";
    } catch {
      return "auto";
    }
  }

  function writeStoredTheme(mode) {
    try {
      if (typeof localStorage !== "object" || !localStorage) return;
      if (mode === "auto") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, mode);
    } catch {
      // Storage unavailable (private mode or restricted iframe); keep in-memory state.
    }
  }

  function applyTheme(mode) {
    const valid = THEMES.includes(mode) ? mode : "auto";
    const docEl = typeof document === "object" && document ? document.documentElement : null;
    if (docEl && docEl.dataset) {
      if (valid === "auto") delete docEl.dataset.theme;
      else docEl.dataset.theme = valid;
    }
    const button = typeof document === "object" && document && document.getElementById
      ? document.getElementById("theme-toggle") : null;
    if (button) {
      const label = valid === "auto" ? "Auto" : valid === "light" ? "Light" : "Dark";
      const next = THEMES[(THEMES.indexOf(valid) + 1) % THEMES.length];
      button.textContent = `Theme: ${label}`;
      button.dataset.themeMode = valid;
      button.setAttribute("aria-label", `Color theme: ${label}. Switch to ${next}`);
    }
    return valid;
  }

  function renderVersion(version, repository) {
    if (typeof version !== "string" || !/^\d+(\.\d+)*[0-9A-Za-z.+!-]*$/.test(version)) {
      return "lu-machinery version unknown";
    }
    const label = `lu-machinery ${escapeHtml(version)}`;
    if (typeof repository !== "string" || !/^[A-Za-z0-9-]{1,39}\/(?!\.\.?$)[A-Za-z0-9._-]{1,100}$/.test(repository)) {
      return label;
    }
    const changelog = `https://github.com/${repository}/blob/main/CHANGELOG.md`;
    return `<a href="${changelog}#v${encodeURIComponent(version)}" title="What changed in this release">${label}</a>`;
  }

  function loadVersion() {
    if (typeof document !== "object" || !document || !document.getElementById) return;
    const header = document.getElementById("machinery-version");
    if (!header) return;
    header.innerHTML = renderVersion(header.dataset?.version, header.dataset?.repository);
  }

  function initChrome() {
    if (typeof document !== "object" || !document || !document.getElementById) return;
    let mode = applyTheme(readStoredTheme());
    const toggle = document.getElementById("theme-toggle");
    if (toggle && !toggle.dataset.bound && typeof toggle.addEventListener === "function") {
      toggle.dataset.bound = "1";
      toggle.addEventListener("click", () => {
        mode = THEMES[(THEMES.indexOf(mode) + 1) % THEMES.length];
        writeStoredTheme(mode);
        applyTheme(mode);
      });
    }
    loadVersion();
  }

  // ---- Fetch & formatting ----

  // null: this runtime has no monotonic clock, so the caller keeps the wall-clock receipt.
  // NaN: a monotonic clock exists but did not return a finite stamp. That receipt stays
  // unknown. Dropping it would fall through to a wall clock that can jump back to fresh.
  function snapshotClockStamp() {
    const perf = typeof performance === "object" && performance ? performance : null;
    if (!perf || typeof perf.now !== "function") return null;
    const value = perf.now();
    return typeof value === "number" && Number.isFinite(value) ? value : Number.NaN;
  }

  async function fetchJson(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
    try {
      const response = await fetch(url, {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
      // Stamp when the response arrives, not when the request starts, and not after the body is parsed.
      const receivedAt = Date.now();
      const receivedMono = snapshotClockStamp();
      let data;
      try { data = JSON.parse(await response.text()); } catch { throw new Error(`${url}: Invalid JSON`); }
      if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error(`${url}: Invalid JSON shape`);
      const headers = response.headers && typeof response.headers.get === "function" ? response.headers : null;
      // Keep the raw header. Number("") is 0, and 0 is a real age, so parsing happens in panelAgeSeconds.
      const state = headers ? headers.get("X-Snapshot-State") : null;
      return {
        data,
        ageHeader: headers ? headers.get("X-Snapshot-Age-S") : null,
        boundHeader: headers ? headers.get("X-Snapshot-Stale-After-S") : null,
        state,
        snapshot: true,
        receivedAt,
        ...(receivedMono === null ? {} : {receivedMono}),
        url,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const moment = typeof value === "number" ? new Date(value < 1e11 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(moment.getTime()) ? null : moment;
  }

  function utc(value) {
    const moment = toDate(value);
    return moment ? moment.toISOString().slice(0, 16).replace("T", " ") + "Z" : "—";
  }

  function span(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
    const s = Math.abs(Math.round(seconds));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
  }

  function relative(value) {
    const moment = toDate(value);
    if (!moment) return "";
    const seconds = (moment.getTime() - Date.now()) / 1000;
    return seconds >= 0 ? `in ${span(seconds)}` : `${span(seconds)} ago`;
  }

  function ageSpan(value) {
    const moment = toDate(value);
    return moment ? span((Date.now() - moment.getTime()) / 1000) : null;
  }

  function validPct(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
  }

  function pct(value) {
    return validPct(value) ? `${Math.round(value)}%` : "—";
  }

  function bar(left) {
    if (!validPct(left)) return "";
    const color = left < 15 ? "var(--bad)" : left < 35 ? "var(--warn)" : "var(--ok)";
    return `<span class="bar"><span style="width:${left}%;background:${color}"></span></span>`;
  }

  // A cell carries its column name, so a stacked (card) layout can show it beside the value.
  // The first cell is the card's own heading, so it keeps no label.
  function labelled(cells, headers) {
    return cells.map((cell, index) => index && headers[index]
      ? cell.replace(/^<td/, `<td data-label="${escapeHtml(headers[index])}"`) : cell).join("");
  }

  function table(headers, rows, stacked = false) {
    if (!rows.length) return '<p class="muted">None.</p>';
    return `<table><thead><tr>${headers.map(h => `<th scope="col">${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>`
      + rows.map(cells => `<tr>${stacked ? labelled(cells, headers) : cells.join("")}</tr>`).join("") + "</tbody></table>";
  }

  const td = (html, cls = "") => `<td${cls ? ` class="${cls}"` : ""}>${html}</td>`;
  const text = (value, cls = "") => td(value === null || value === undefined || value === "" ? "—" : escapeHtml(value), cls);
  const unknown = reason => `<p class="muted">Unknown: ${escapeHtml(String(reason || "no reason given").replaceAll("_", " "))}.</p>`;
  const clockUtc = (moment = Date.now()) => new Date(moment).toISOString().slice(11, 19) + " UTC";

  function chip(href, count, label, cls = "", key = "") {
    const keyAttr = key ? ` data-chip-key="${escapeHtml(key)}"` : "";
    return `<a class="chip${cls ? ` ${cls}` : ""}"${keyAttr} href="${escapeHtml(href)}"><b>${escapeHtml(count)}</b> ${escapeHtml(label)}</a>`;
  }

  // ---- Shared domain helpers (subscriptions & PRs) ----

  function readingState(freshness, ageS, thresholdS) {
    const aged = typeof ageS === "number" && Number.isFinite(ageS) && ageS >= 0;
    const limit = typeof thresholdS === "number" && thresholdS > 0 ? thresholdS : 900;
    if (freshness === "stale_last_good") return "last_good";
    if (freshness === "stale" || ((freshness === "fresh") && aged && ageS > limit)) return "stale";
    if (freshness === "fresh") return aged ? "fresh" : "unknown";
    return freshness ? String(freshness) : "missing";
  }

  function freshnessCell(state, ageS) {
    const age = typeof ageS === "number" && Number.isFinite(ageS) ? `${span(ageS)} old` : "age unknown";
    if (state === "fresh") return text(`fresh, ${age}`, "ok");
    if (state === "last_good") return text(`Last good reading, ${age}`, "warn");
    if (state === "stale") return text(`STALE, ${age}`, "bad");
    if (state === "unknown") return text(`FRESHNESS UNKNOWN, ${age}`, "bad");
    return text(state === "missing" ? "NO READING" : state.toUpperCase(), "bad");
  }

  function pp(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${Math.round(Math.abs(value))} pp` : null;
  }

  function windowLeft(window) {
    if (!window || window.status === "none") return text("none", "muted");
    if (window.status !== "ok" || !validPct(window.left_pct)) return text("unknown", "muted");
    return td(`${bar(window.left_pct)}${escapeHtml(pct(window.left_pct))}`, "num");
  }

  function paceCell(pace) {
    if (!pace || pace.status !== "ok") return text("unknown", "muted");
    const head = pace.state === "on_pace" ? "on pace"
      : pace.state === "reserve" ? `${pp(pace.headroom_pct)} headroom` : `${pp(pace.headroom_pct)} deficit`;
    const tail = pace.lasts_to_reset === true ? "lasts to reset"
      : pace.lasts_to_reset === false ? (!toDate(pace.runs_out_at) ? "runs out before reset"
        : toDate(pace.runs_out_at).getTime() <= Date.now() ? `ran out ${utc(pace.runs_out_at)} (${relative(pace.runs_out_at)})`
        : `runs out ${utc(pace.runs_out_at)} (${relative(pace.runs_out_at)})`)
      : "last-to-reset unknown";
    // The routing status reads the same verdict: running out before the reset is bad at any headroom.
    return text(`${head} · ${tail}`, pace.lasts_to_reset === false ? "bad" : pace.state === "deficit" ? "warn" : "ok");
  }

  function readingCell(reading, staleAfter) {
    const state = readingState(reading?.status, reading?.age_s, staleAfter);
    if (state === "missing" || state === "unknown" && !reading?.read_at) {
      return text(`NO READING${reading?.reason_code ? ` (${String(reading.reason_code).replaceAll("_", " ")})` : ""}`, "bad");
    }
    return freshnessCell(state, reading?.age_s);
  }

  // Sign-in state joins the subscriptions row by account label ("Claude a").
  function signInCell(account, logins) {
    if (!logins) return text("unknown", "muted");
    if (logins.state === "pending") return text("checking…", "muted");
    const row = (Array.isArray(logins.accounts) ? logins.accounts : []).find(item => item.account === account);
    if (!row) return text("no sign-in reader", "muted");
    if (row.logged_in === false) return text("SIGNED OUT", "bad");
    if (row.logged_in !== true) return text(`UNKNOWN${row.reason_code ? ` (${String(row.reason_code).replaceAll("_", " ")})` : ""}`, "bad");
    const expires = toDate(row.expires_at);
    if (!expires) return text("signed in", "ok");
    const daysLeft = (expires.getTime() - Date.now()) / 86400000;
    return text(`signed in · ends ${relative(row.expires_at)}`, daysLeft < 3 ? "bad" : daysLeft < 7 ? "warn" : "ok");
  }

  function liveAccounts(subs) {
    return (Array.isArray(subs?.providers) ? subs.providers : [])
      .flatMap(provider => Array.isArray(provider.accounts) ? provider.accounts : []).filter(row => !row.plan?.ended);
  }

  function summarizeSubscriptions(subs, logins) {
    const accounts = liveAccounts(subs);
    let runsOut = 0, unknownCount = 0, deficitCount = 0, healthyCount = 0;
    const staleAfter = subs?.stale_after_s;
    for (const row of accounts) {
      const state = readingState(row.reading?.status, row.reading?.age_s, staleAfter);
      const pace = row.pace;
      const answered = state === "fresh" && pace && pace.status === "ok" && typeof pace.lasts_to_reset === "boolean";
      if (!answered) { unknownCount += 1; continue; }
      if (pace.lasts_to_reset === false) runsOut += 1;
      else if (pace.state === "deficit") deficitCount += 1;
      else healthyCount += 1;
    }
    const signedOut = (Array.isArray(logins?.accounts) ? logins.accounts : []).filter(row => row.logged_in === false).length;
    return {total: accounts.length, runsOut, unknownCount, deficitCount, healthyCount, signedOut};
  }

  function subscriptionChips(subs, logins, href = "/models#budget") {
    if (!subs) return [];
    const {runsOut, unknownCount, signedOut} = summarizeSubscriptions(subs, logins);
    const noun = runsOut === 1 ? "account runs out before reset" : "accounts run out before reset";
    const out = [chip(href, runsOut, noun + (unknownCount ? `, ${unknownCount} unknown` : ""),
      runsOut ? "bad" : unknownCount ? "warn" : "")];
    if (signedOut) out.push(chip(href, signedOut, "signed out", "bad"));
    return out;
  }

  function ciLabel(state) {
    if (state === "success") return ["CI green", "ok"];
    if (state === "failure" || state === "error") return ["CI red", "bad"];
    if (state === "pending" || state === "expected") return ["CI running", "warn"];
    return ["no CI yet", "muted"];
  }

  function reviewLabel(state) {
    if (state === "success") return ["approved", "ok"];
    if (state === "failure" || state === "error") return ["changes / refused", "bad"];
    if (state === "pending") return ["pending", "warn"];
    return ["none", "muted"];
  }

  function prLink(repo, pr) {
    const label = `${repo}#${pr.number}`;
    const safe = safeHttpUrl(pr.url);
    return safe ? `<a href="${escapeHtml(safe)}">${escapeHtml(label)}</a>` : escapeHtml(label);
  }

  function repoCoverageIncomplete(repo) {
    if (!repo || repo.status !== "ok") return true;
    const list = Array.isArray(repo.pull_requests) ? repo.pull_requests : [];
    return typeof repo.open_total !== "number" || !Number.isFinite(repo.open_total) || repo.open_total > list.length;
  }

  function repoOpenCount(repo) {
    if (!repo || repo.status !== "ok") return 0;
    const list = Array.isArray(repo.pull_requests) ? repo.pull_requests : [];
    return typeof repo.open_total === "number" && Number.isFinite(repo.open_total) && repo.open_total >= 0
      ? Math.max(repo.open_total, list.length) : list.length;
  }

  function normalizedState(value) {
    return typeof value === "string" ? value.trim().toLowerCase() : "";
  }

  function isPullRequestUrl(url) {
    return url === "/api/git/pull-requests" || (typeof url === "string" && url.startsWith("/api/git/pull-requests?"));
  }

  function prSnapshotUnavailable(readingOrData, state = null) {
    const data = readingOrData && Object.hasOwn(readingOrData, "data") ? readingOrData.data : readingOrData;
    const snapState = normalizedState(state ?? (readingOrData && Object.hasOwn(readingOrData, "state") ? readingOrData.state : null));
    if (!data || data.status !== "ok" || snapState === "unavailable") return true;
    const repos = Array.isArray(data.repositories) ? data.repositories : null;
    return Boolean(repos && repos.length > 0 && !repos.some(repo => repo && repo.status === "ok"));
  }

  function prChips(prs, href = "/pull-requests", prState = null) {
    if (!prs) return [];
    const repos = Array.isArray(prs.repositories) ? prs.repositories : [];
    const unavailable = prState === "unavailable" || prSnapshotUnavailable(prs, prState);
    if (unavailable) return [chip(href, "?", "PRs unknown", "warn")];
    const expired = prState === "expired" || prState === true;
    const ageUnknown = prState === "unknown";
    const unbounded = prState === "unbounded";
    const open = repos.reduce((sum, repo) => sum + repoOpenCount(repo), 0);
    const queued = repos.reduce((sum, repo) => sum + (repo.status === "ok" ? (repo.pull_requests || []).filter(pr => pr.queue).length : 0), 0);
    const partial = repos.some(repo => repo.status !== "ok");
    const truncated = repos.some(repo => repo.status === "ok" && repoCoverageIncomplete(repo));
    const notes = [];
    if (expired) notes.push("cached");
    if (ageUnknown) notes.push("age unknown");
    if (unbounded) notes.push("freshness unknown");
    if (partial) notes.push("a repository is unknown");
    if (truncated) notes.push("coverage truncated");
    const openText = notes.length ? "open PRs observed" : "open PRs";
    const queuedText = notes.length ? `${queued} queued observed` : `${queued} queued`;
    const suffix = notes.length ? ` (${notes.join(", ")})` : "";
    return [chip(href, open, `${openText} · ${queuedText}${suffix}`, notes.length ? "warn" : "")];
  }

  // ---- Panel age: one helper for every page ----
  //
  // A panel's age is X-Snapshot-Age-S plus the time since this response was received.
  // A missing, empty, or non-decimal header is unknown. Number("") is 0, so the raw
  // header is parsed here and an empty value never becomes a fresh zero-second age.
  // Payload timestamps (observed_at, read_at) are not an age.

  function headerRaw(reading, headerKey, parsedKey) {
    if (reading && Object.hasOwn(reading, headerKey)) return reading[headerKey];
    return reading ? reading[parsedKey] : undefined;
  }

  function parseSnapshotSeconds(raw) {
    if (typeof raw === "number") return Number.isFinite(raw) && raw >= 0 ? raw : null;
    if (typeof raw !== "string") return null;
    const text = raw.trim();
    if (!/^\d+(?:\.\d+)?$/.test(text)) return null;
    const value = Number(text);
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  function heldElapsed(reading, elapsed) {
    const prior = reading && reading._elapsedS;
    const stable = typeof prior === "number" && Number.isFinite(prior) ? Math.max(prior, elapsed) : elapsed;
    if (reading && typeof reading === "object") reading._elapsedS = stable;
    return stable;
  }

  function panelAgeSeconds(reading, now = Date.now()) {
    const headerAge = parseSnapshotSeconds(headerRaw(reading, "ageHeader", "age"));
    if (headerAge === null) return null;
    if (typeof now !== "number" || !Number.isFinite(now)) return null;
    const received = reading && reading.receivedAt;
    // A non-finite receipt is a broken clock, not "just arrived". Missing receipt stays elapsed 0
    // so a fixture that only carries the header still shows that header.
    if (typeof received === "number" && !Number.isFinite(received)) return null;
    const receivedMono = reading && reading.receivedMono;
    if (typeof receivedMono === "number") {
      if (!Number.isFinite(receivedMono)) return null;
      const monoNow = snapshotClockStamp();
      if (typeof monoNow !== "number" || !Number.isFinite(monoNow)) return null;
      return headerAge + heldElapsed(reading, Math.max(0, (monoNow - receivedMono) / 1000));
    }
    const elapsed = typeof received === "number" ? Math.max(0, (now - received) / 1000) : 0;
    return headerAge + heldElapsed(reading, elapsed);
  }

  function panelBoundSeconds(reading) {
    const bound = parseSnapshotSeconds(headerRaw(reading, "boundHeader", "bound"));
    return bound !== null && bound > 0 ? bound : null;
  }

  function formatSnapshotSeconds(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "unknown";
    return `${Math.floor(value)}s`;
  }

  function formatBoundSeconds(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "unknown";
    const rounded = Math.round(value * 1000) / 1000;
    return `${rounded}s`;
  }

  function panelFreshness(reading, now = Date.now()) {
    const age = panelAgeSeconds(reading, now);
    const bound = panelBoundSeconds(reading);
    const snapState = normalizedState(reading && reading.state);
    const noData = !reading || reading.data == null;
    const unavailable = noData || snapState === "unavailable"
      || (isPullRequestUrl(reading && reading.url) && prSnapshotUnavailable(reading));
    if (unavailable) return {state: "unavailable", age, bound};
    if (age === null) return {state: "unknown", age: null, bound};
    if (snapState === "stale" || (bound !== null && age > bound)) return {state: "stale", age, bound};
    if (bound === null) return {state: "unknown", age, bound: null};
    return {state: "fresh", age, bound};
  }

  function prSnapshotState(reading, now = Date.now()) {
    if (!reading || !reading.data || prSnapshotUnavailable(reading)) {
      return {status: "unavailable", expired: false, unavailable: true};
    }
    const age = panelAgeSeconds(reading, now);
    const bound = panelBoundSeconds(reading);
    // Missing age and a missing bound are different. The body must not say the age is unknown
    // when the header age was measured and only the bound is unusable.
    if (age === null) return {status: "unknown", reason: "age", expired: false, unavailable: false};
    if (bound === null) return {status: "unbounded", reason: "bound", expired: false, unavailable: false};
    const expired = age > bound;
    return {status: expired ? "expired" : "fresh", expired, unavailable: false};
  }

  function prSnapshotFresh(reading, now = Date.now()) {
    return prSnapshotState(reading, now).status === "fresh";
  }

  function prSnapshotExpired(reading, now = Date.now()) {
    return prSnapshotState(reading, now).expired;
  }

  const FRESHNESS_LABELS = {
    fresh: "fresh",
    stale: "STALE",
    unknown: "freshness unknown",
    unavailable: "unavailable",
  };

  const DEFAULT_SOURCE_LABELS = ["Usage", "API balances", "Sign-in"];

  function headingAge(readings, badOnFailure, attemptedAt = null, sourceLabels = DEFAULT_SOURCE_LABELS, now = Date.now()) {
    const sources = Array.isArray(readings) ? readings : [];
    const moment = sources[0]?.receivedAt ?? attemptedAt;
    const label = sources.length ? "Fetched" : "Last attempt";
    const fetched = moment ? `${label} at ${clockUtc(moment)}` : `${label} time unknown`;
    if (!sources.length) return {text: `${fetched} · unavailable · source age unknown`, bad: Boolean(badOnFailure)};
    let bad = false, caution = false;
    const labels = sources.map((reading, index) => {
      const fresh = panelFreshness(reading, now);
      bad ||= fresh.state === "stale" || fresh.state === "unavailable";
      caution ||= fresh.state === "unknown";
      const prefix = sources.length > 1 ? `${(sourceLabels && sourceLabels[index]) || "Source"}: ` : "";
      return `${prefix}${FRESHNESS_LABELS[fresh.state]} · source age ${formatSnapshotSeconds(fresh.age)}`
        + ` · bound ${formatBoundSeconds(fresh.bound)}`;
    });
    return {text: `${fetched} · ${labels.join("; ")}`, bad, caution};
  }

  // ---- Panel error handling, announcements & runner ----

  function isRefusal(error) {
    const message = String(error && error.message ? error.message : error);
    const status = /HTTP (\d{3})/.exec(message);
    return Boolean(status) && (status[1] === "401" || status[1] === "403");
  }

  function failureSentence(error) {
    const message = String(error && error.message ? error.message : error);
    const status = /HTTP (\d{3})/.exec(message);
    if (status) {
      const code = Number(status[1]);
      if (code === 401 || code === 403) return "This panel couldn't load: the source refused the request.";
      if (code === 404) return "This panel couldn't load: the source was not found.";
      if (code >= 500) return "This panel couldn't load: the source failed.";
      return "This panel couldn't load: the source rejected the request.";
    }
    if (/abort|timeout|timed out/i.test(message)) return "This panel couldn't load: the source did not answer in time.";
    if (/JSON/i.test(message)) return "This panel couldn't load: the source returned data this page could not read.";
    return "This panel couldn't load.";
  }

  function panelError(panel, error) {
    const name = escapeHtml(panel.label || panel.id);
    const detail = escapeHtml(error && error.message ? error.message : error);
    return `<p class="error">${failureSentence(error)}</p>`
      + `<p><button type="button" class="retry" data-retry-panel="${escapeHtml(panel.id)}" aria-label="Retry ${name}">Retry</button></p>`
      + `<details><summary>Technical detail</summary><p class="muted">${detail}</p></details>`;
  }

  function panelUnavailable() {
    return '<p class="muted">This panel isn\'t available in this view.</p>';
  }

  function announce(panel, state) {
    if (panel.state === state) return;
    panel.state = state;
    const region = document.getElementById(`${panel.id}-status`);
    if (!region) return;
    const name = panel.label || panel.id;
    region.textContent = state === "loading" ? `Loading ${name}.`
      : state === "ready" ? `${name} loaded.`
      : state === "unavailable" ? `${name} isn't available in this view.`
      : `${name} couldn't load.`;
  }

  function updatePanelAge(panel, now = Date.now()) {
    const age = document.getElementById(`${panel.id}-age`);
    if (!age) return;
    const line = headingAge(panel.readings, panel.ok === false && !panel.refusal, panel.attemptedAt, panel.sourceLabels, now);
    age.textContent = line.text;
    age.className = line.bad ? "age bad" : line.caution ? "age warn" : "age";
  }

  function showRefresh(panels) {
    const status = document.getElementById("last-refresh");
    const retry = document.getElementById("refresh-retry");
    if (!status) return;
    const realFailure = panels.some(panel => panel.ok === false && panel.refusal !== true);
    const anyLoaded = panels.some(panel => panel.ok === true);
    if (!realFailure) {
      status.textContent = anyLoaded ? `Updated ${clockUtc()} · every 30 s` : `Nothing is available in this view, ${clockUtc()}`;
      if (retry) retry.hidden = true;
      return;
    }
    status.textContent = `Couldn't refresh, last attempt ${clockUtc()}`;
    if (retry) retry.hidden = false;
  }

  const DEFAULT_OPTIONAL = new Set(["/api/git/pull-requests", "/api/state/routing-budget", "/api/runtime/logins"]);

  function createPanelRunner(options) {
    const panels = Array.isArray(options?.panels) ? options.panels : [];
    const optionalUrls = options?.optionalUrls || DEFAULT_OPTIONAL;
    const renderChips = typeof options?.renderChips === "function" ? options.renderChips : null;
    const onAfterLoad = typeof options?.onAfterLoad === "function" ? options.onAfterLoad : null;
    const onRefresh = typeof options?.onRefresh === "function" ? options.onRefresh : null;
    const fetchImpl = typeof options?.fetchJson === "function" ? options.fetchJson : fetchJson;
    const latest = {};
    const inFlight = new Set();

    function readSource(url, index) {
      if (index === 0 || !optionalUrls.has(url)) return fetchImpl(url);
      const missing = {data: null, age: null, snapshot: false, url};
      return Promise.race([
        fetchImpl(url).catch(() => missing),
        new Promise(resolve => setTimeout(() => resolve(missing), OPTIONAL_WAIT_MS)),
      ]);
    }

    function updateChips() {
      if (!renderChips) return;
      const box = document.getElementById("chips");
      if (!box) return;
      const nextHtml = renderChips(latest);
      if (box.innerHTML === nextHtml) return;
      const active = typeof document === "object" && document ? document.activeElement : null;
      let focusState = null;
      if (active && typeof box.contains === "function" && box.contains(active)) {
        const currentChips = typeof box.querySelectorAll === "function" ? [...box.querySelectorAll("a.chip")] : [];
        const chipKey = active.dataset?.chipKey || "";
        const href = typeof active.getAttribute === "function" ? (active.getAttribute("href") || "") : "";
        const sameHref = href ? currentChips.filter(node => typeof node.getAttribute === "function" && node.getAttribute("href") === href) : [];
        focusState = {
          chipKey,
          href,
          sameHrefIndex: sameHref.indexOf(active),
          chipIndex: currentChips.indexOf(active),
        };
      }
      box.innerHTML = nextHtml;
      if (focusState && typeof box.querySelectorAll === "function") {
        const rebuilt = [...box.querySelectorAll("a.chip")];
        let target = null;
        if (focusState.chipKey) {
          target = rebuilt.find(node => node.dataset?.chipKey === focusState.chipKey) || null;
        }
        if (!target && focusState.href) {
          const sameHref = rebuilt.filter(node => typeof node.getAttribute === "function" && node.getAttribute("href") === focusState.href);
          target = sameHref[focusState.sameHrefIndex] || sameHref[0] || null;
        }
        if (!target && rebuilt.length && focusState.chipIndex >= 0) {
          target = rebuilt[Math.min(focusState.chipIndex, rebuilt.length - 1)] || null;
        }
        if (!target && typeof document === "object" && document && typeof document.getElementById === "function") {
          const retry = document.getElementById("refresh-retry");
          target = (retry && !retry.hidden ? retry : null)
            || document.getElementById("theme-toggle")
            || document.getElementById("main");
        }
        if (target && typeof target.focus === "function") {
          try { target.focus({preventScroll: true}); } catch { target.focus(); }
        }
      }
    }

    async function loadPanel(panel) {
      if (inFlight.has(panel.id)) return;
      inFlight.add(panel.id);
      panel.attemptedAt = Date.now();
      const content = document.getElementById(`${panel.id}-content`);
      const age = document.getElementById(`${panel.id}-age`);
      if (age && !age.textContent) age.textContent = "Loading…";
      if (panel.state !== "ready") announce(panel, "loading");
      try {
        const readings = await Promise.all(panel.urls.map(readSource));
        const data = readings.map(reading => reading.data);
        panel.readings = readings;
        const renderNow = Date.now();
        const renderArgs = panel.withReadings ? [...data, readings, renderNow] : data;
        if (content) content.innerHTML = panel.render(...renderArgs);
        latest[panel.id] = data[0];
        if (typeof panel.onData === "function") panel.onData(data, latest, readings, renderNow);
        if (onAfterLoad) onAfterLoad(panel, data, latest);
        const ageLine = headingAge(readings, false, panel.attemptedAt, panel.sourceLabels, renderNow);
        if (age) {
          age.textContent = ageLine.text;
          age.className = ageLine.bad ? "age bad" : ageLine.caution ? "age warn" : "age";
        }
        panel.ok = true;
        panel.refusal = false;
        announce(panel, "ready");
        if (panel.retry && panel.retry(...data)) {
          panel.retries = (panel.retries || 0) + 1;
          if (panel.retries <= RETRY_LIMIT) setTimeout(() => loadPanel(panel), 3000);
        } else {
          panel.retries = 0;
        }
      } catch (error) {
        const refused = isRefusal(error);
        if (content) content.innerHTML = refused ? panelUnavailable() : panelError(panel, error);
        const ageLine = headingAge(null, !refused, panel.attemptedAt, panel.sourceLabels);
        panel.readings = null;
        if (age) {
          age.textContent = ageLine.text;
          age.className = ageLine.bad ? "age bad" : "age";
        }
        latest[panel.id] = null;
        if (typeof panel.onError === "function") panel.onError(latest);
        panel.ok = false;
        panel.refusal = refused;
        announce(panel, refused ? "unavailable" : "error");
      } finally {
        inFlight.delete(panel.id);
      }
    }

    async function retryPanel(id) {
      const panel = panels.find(item => item.id === id);
      if (!panel) return;
      await loadPanel(panel);
      updateChips();
      showRefresh(panels);
    }

    async function refreshAll() {
      loadVersion();
      if (onRefresh) onRefresh();
      await Promise.all(panels.map(loadPanel));
      updateChips();
      showRefresh(panels);
    }

    function bindEvents() {
      if (typeof document !== "object" || !document) return;
      const retryBtn = document.getElementById("refresh-retry");
      if (retryBtn && !retryBtn.dataset?.bound && typeof retryBtn.addEventListener === "function") {
        if (retryBtn.dataset) retryBtn.dataset.bound = "1";
        retryBtn.addEventListener("click", () => { refreshAll(); });
      }
      if (typeof document.addEventListener === "function" && !document._machineryRetryBound) {
        document._machineryRetryBound = true;
        document.addEventListener("click", event => {
          const target = event.target && typeof event.target.closest === "function"
            ? event.target.closest("button[data-retry-panel]") : null;
          if (target) retryPanel(target.dataset.retryPanel);
        });
      }
    }

    function start() {
      initChrome();
      bindEvents();
      refreshAll();
      setInterval(refreshAll, REFRESH_MS);
      // This clock advances even if a refresh stalls or the network disappears. It makes no requests.
      setInterval(() => {
        const now = Date.now();
        panels.filter(panel => panel.ok !== undefined).forEach(panel => {
          updatePanelAge(panel, now);
          if (typeof panel.onTick === "function") panel.onTick(panel, latest, updateChips, now);
        });
      }, 1000);
    }

    return {panels, latest, loadPanel, retryPanel, refreshAll, start};
  }

  const Machinery = {
    API_TIMEOUT_MS,
    REFRESH_MS,
    OPTIONAL_WAIT_MS,
    RETRY_LIMIT,
    FRESHNESS_LABELS,
    DEFAULT_OPTIONAL,
    escapeHtml,
    safeHttpUrl,
    applyTheme,
    renderVersion,
    loadVersion,
    initChrome,
    fetchJson,
    toDate,
    utc,
    span,
    relative,
    ageSpan,
    validPct,
    pct,
    bar,
    labelled,
    table,
    td,
    text,
    unknown,
    clockUtc,
    chip,
    readingState,
    freshnessCell,
    pp,
    windowLeft,
    paceCell,
    readingCell,
    signInCell,
    liveAccounts,
    summarizeSubscriptions,
    subscriptionChips,
    ciLabel,
    reviewLabel,
    prLink,
    repoCoverageIncomplete,
    repoOpenCount,
    prChips,
    snapshotClockStamp,
    panelAgeSeconds,
    panelBoundSeconds,
    prSnapshotState,
    prSnapshotFresh,
    prSnapshotExpired,
    headingAge,
    isRefusal,
    failureSentence,
    panelError,
    panelUnavailable,
    announce,
    updatePanelAge,
    showRefresh,
    createPanelRunner,
  };

  if (typeof root === "object" && root) root.Machinery = Machinery;
  if (typeof module !== "undefined" && module.exports) module.exports = Machinery;
})(typeof globalThis !== "undefined" ? globalThis : this);
