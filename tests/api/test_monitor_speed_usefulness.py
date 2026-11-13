"""Behavioral regressions for Monitor loading and honest queue evidence (#7684)."""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from lu_machinery.api import work_router
from lu_machinery.api.main import app
from lu_machinery.common.repo_root import machinery_path



def _node(script: str) -> None:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _script(page: str, start: str, end: str) -> str:
    html = (machinery_path("api/dashboards") / page).read_text()
    return html[html.index(start) : html.index(end, html.index(start))]


@pytest.mark.parametrize("source_status", ["ok", "degraded", "unavailable"])
def test_next_empty_queue_preserves_source_completeness(monkeypatch, source_status):
    source = {
        "source_id": "public-monitor",
        "status": source_status,
        "sections": {"issues": {"status": "ok" if source_status == "ok" else "timeout", "count": 0}},
    }
    denominator = {
        "issues_open": 0,
        "omissions": [] if source_status == "ok" else [{"class": "issues", "reason": "timeout"}],
    }
    payload = {
        "items": [],
        "sources": [source, {"source_id": "private-local-adapter", "status": "unavailable"}],
        "denominator": denominator,
        "generated_at": "2026-09-04T00:00:00Z",
    }
    monkeypatch.setattr(work_router, "_known_streams", lambda _ctx: ["infra-harness"])
    monkeypatch.setattr(work_router, "cache_get_with_age", lambda *_args: (payload, 1.0))
    response = TestClient(app).get("/api/work/v1/next?stream=infra-harness")
    assert response.status_code == 200
    assert response.json()["queue"] == []
    assert response.json()["sources"] == [source]
    assert response.json()["denominator"] == denominator


def test_projection_direct_json_retains_opsec_middleware(monkeypatch):
    payload = {
        "items": [
            {
                "title": "/home/synthetic-private/data.json",
                "projections": {"stream": {"streams": ["infra-harness"]}},
            }
        ],
        "attention": [],
        "cache_age_s": 0,
    }
    monkeypatch.setattr(work_router, "cache_get_with_age", lambda *_args: (payload, 1.0))
    response = TestClient(app).get("/api/work/v1/projection?stream=infra-harness")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["title"] == "[redacted-path]"
    assert "/home/synthetic-private" not in response.text
    assert response.json()["cache_age_s"] == 1.0
    assert payload["items"][0]["title"] == "/home/synthetic-private/data.json"


_DOM = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {textContent: 'old data', innerHTML: 'old data', className: '',
    setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}});
  return elements.get(id);
}};
const tick = () => new Promise(resolve => setImmediate(resolve));
"""


def test_home_panels_paint_independently_and_a_failed_panel_clears_old_data():
    code = _script("index.html", "const inFlight = new Set();", "\nasync function refreshAll")
    _node(
        _DOM
        + r"""
const calls = [];
const REFRESH_MS = 30000;
const latest = {};
const OPTIONAL = new Set();
let loginPolls = 0;
const setTimeout = (fn, ms) => {if (ms === 3000) loginPolls++;};
let resolveSlow;
const slow = new Promise(resolve => {resolveSlow = resolve;});
const fetchJson = async url => {
  calls.push(url);
  if (url === '/slow') return slow;
  if (url === '/broken') throw new Error('/broken: HTTP 503');
  if (url === '/login') return {data: {state: 'pending'}, age: null, snapshot: false};
  return {data: {tasks: []}, age: 900, snapshot: true};
};
const escapeHtml = value => String(value);
const PANELS = [
  {id: 'fast', urls: ['/fast'], render: data => `fast ${data.tasks.length}`},
  {id: 'broken', urls: ['/broken'], render: () => 'never'},
  {id: 'slow', urls: ['/slow'], render: data => `slow ${data.marker}`},
  {id: 'logins', urls: ['/login'], render: data => data.state, retry: data => data.state === 'pending'},
];
"""
        + code
        + r"""
(async () => {
  const first = Promise.all(PANELS.map(loadPanel));
  await tick();
  assert.equal(document.getElementById('fast-content').innerHTML, 'fast 0');
  assert.match(document.getElementById('fast-age').textContent, /^Fetched at \d{2}:\d{2}:\d{2} UTC · source age 900s$/);
  assert.equal(document.getElementById('fast-age').className, 'age bad');
  assert.equal(loginPolls, 1, 'pending login retries independently of another failed panel');
  const broken = document.getElementById('broken-content').innerHTML;
  assert.match(broken, /This panel couldn't load/);
  assert.match(broken, /\/broken: HTTP 503/);
  assert.doesNotMatch(broken.split('<details>')[0], /\/broken/);
  assert.doesNotMatch(broken, /old data/);
  assert.equal(document.getElementById('slow-content').innerHTML, 'old data');
  await loadPanel(PANELS[2]);
  assert.equal(calls.filter(url => url === '/slow').length, 1, 'overlap must not duplicate requests');
  resolveSlow({data: {marker: 'new'}, age: null, snapshot: false});
  await first;
  assert.equal(document.getElementById('slow-content').innerHTML, 'slow new');
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    )
    page = machinery_path("api/dashboards/index.html").read_text()
    script = page.split("<script>", 1)[1].split("\nrefreshAll();", 1)[0]
    _node(_DOM + script + r"""
const recent = renderRecent({tasks: [{task_id: 'finished', status: 'done'}],
  needs_attention: {total: 1530, tasks: [{task_id: 'unfinished', status: 'needs_finalize'},
    {task_id: 'crashed', status: 'zombie'}]}});
assert.match(recent, /1530 unfinished task records need attention/);
assert.match(recent, /Showing up to 2/);
assert.match(recent, /needs_finalize/);
assert.match(recent, /zombie/);
assert.match(recent, /finished/);
assert.doesNotMatch(renderWorkers({tasks: []}), /unfinished|zombie/);
""")


def test_home_hides_stale_subscription_quota_and_api_balances():
    """Stale or unverified readings must not render actionable quota, pace, reset or balance numbers."""
    code = _script("index.html", "function escapeHtml", "// ---- Panels ----")
    _node(_DOM + code + r"""
const row = {account: 'Fixture', reading: {status: 'fresh', age_s: 60, read_at: '2026-11-12T00:00:00Z'},
  weekly: {status: 'ok', left_pct: 70, resets_at: '2099-01-02T03:04:00Z'},
  pace: {status: 'ok', state: 'reserve', headroom_pct: 18, lasts_to_reset: true},
  session: {status: 'ok', left_pct: 61, resets_at: '2099-01-02T03:04:00Z'},
  extra_windows: [{label: 'Extra', status: 'ok', left_pct: 84}], breakdown: [{label: 'Used', value: '91 units'}],
  credits: {status: 'ok', balance: 37.2}, plan: {name: 'Fixture plan'},
  banked_resets: {status: 'ok', source: 'provider', count: 17,
    items: [{kind: 'reset', count: 17, expires_on: '2099-02-03'}]}};
const render = reading => subscriptionRows({stale_after_s: 900,
  providers: [{provider: 'fixture', accounts: [{...row, reading}]}]}, null).groups;
assert.match(render(row.reading), /70%/);
assert.match(render(row.reading), /17× reset/);
const chips = reading => renderChips({usage: {stale_after_s: 900,
  providers: [{accounts: [{...row, reading}]}]}});
assert.match(chips(row.reading), /<b>0<\/b> accounts run out before reset/);
for (const reading of [{...row.reading, age_s: 1800}, {...row.reading, status: 'stale'},
    {...row.reading, status: 'unknown'}, {...row.reading, age_s: null}]) {
  const html = render(reading);
  assert.equal(/17× reset/.test(html), false);
  assert.doesNotMatch(html, /70%|61%|84%|91 units|37\.2|2099-01-02|18 pp|lasts to reset/);
  assert.match(html, /plan · renewal unknown/);
  assert.match(chips(reading), /<b>0<\/b> accounts run out before reset, 1 unknown/);
  assert.doesNotMatch(chips(reading), /chip bad/);
}
const noReader = renderUsage({stale_after_s: 900, providers: [{provider: 'agy', label: 'AGY', accounts: [row]}], sources: {}},
  null, {state: 'ok', accounts: [{account: 'Claude a', logged_in: true}]});
assert.match(noReader, /no sign-in reader/);
assert.match(noReader, /Claude a, Claude b, Codex a and Codex b only/);
const configured = {...row, reading: {...row.reading, status: 'stale'},
  banked_resets: {...row.banked_resets, source: 'plan_facts'}};
assert.match(subscriptionRows({providers: [{accounts: [configured]}]}, null).groups, /17× reset/);
const apiAccountRow = {freshness: 'fresh', age_s: 60, total_balance: 42, currency: 'USD', usage_weekly_usd: 9};
const api = reading => apiRows({api_accounts: {Fixture: {...apiAccountRow, ...reading}},
  diagnostics: {stale_threshold_s: 900}}).flat().join('');
assert.match(api({}), /42\.00 USD balance/);
assert.match(api({}), /9\.00 this week/);
for (const reading of [{age_s: 1800}, {freshness: 'stale'}, {freshness: 'unknown'}, {age_s: null}])
  assert.doesNotMatch(api(reading), /42\.00|9\.00/);
const lastGood = api({freshness: 'stale_last_good', balance_probe_state: 'NEED_KEY'});
assert.match(lastGood, /42\.00 USD balance/);
assert.match(lastGood, /\$9\.00 this week \(last good\)/);
assert.match(lastGood, /Last good reading, 1m old/);
assert.match(lastGood, /needs an API key/);
assert.doesNotMatch(lastGood, /STALE_LAST_GOOD|NEED_KEY/);
assert.match(lastGood, /class="warn">Last good reading/);
const named = renderAlerts({status: 'ok', heartbeat: true, alerts: [
  {alertname: 'UnrecognizedAlert', severity: 'warning', starts_at: '2026-11-01T00:00:00Z', fingerprint: 'abc'},
  {alertname: 'ApprovedNotLanded30m', severity: 'warning', starts_at: '2026-11-12T00:00:00Z'}]});
assert.match(named, /Unrecognised alert \(fingerprint abc\)/);
assert.match(named, /ApprovedNotLanded30m/);
assert.doesNotMatch(named, /UnrecognizedAlert/);
""")


def test_home_comms_collection_errors_are_unavailable_even_when_store_exists():
    """A collector failure must not appear as successful plane access just because the database exists."""
    code = _script("index.html", "function escapeHtml", "// ---- Summary chips ----")
    _node(_DOM + code + r"""
const metrics = {source: 'authority', store: {reachable: true}, deliveries: {queued: 99}};
assert.match(renderComms(metrics), /authority reachable/);
for (const failure of [{db_error: 'DatabaseError'}, {db_missing: true}]) {
  const html = renderComms({...metrics, ...failure});
  assert.match(html, /unavailable/i);
  assert.doesNotMatch(html, /authority reachable|class="ok"|99/);
}
""")


@pytest.mark.parametrize("body", ["not-json", "null", "[]"])
def test_home_invalid_json_is_an_error(body):
    code = _script("index.html", "async function fetchJson", "\nfunction toDate")
    _node(
        _DOM
        + "const API_TIMEOUT_MS = 1000;\nconst fetch = async () => ({ok: true, text: async () => "
        + json.dumps(body)
        + "});\n"
        + code
        + r"""
assert.rejects(fetchJson('/fixture'), /Invalid/).catch(e => {console.error(e); process.exitCode = 1;});
"""
    )


def test_failing_panel_shows_a_calm_sentence_retry_and_hidden_detail():
    """Defect class: a failed panel shows a raw API path, omits Retry, or leaves its age blank."""
    code = _script("index.html", "const inFlight = new Set();", "\nasync function refreshAll")
    _node(
        _DOM
        + r"""
const REFRESH_MS = 30000;
const latest = {};
const OPTIONAL = new Set();
const setTimeout = () => {};
const fetchJson = async url => { throw new Error(`${url}: HTTP 503`); };
const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const PANELS = [{id: 'usage', label: 'Subscriptions', urls: ['/api/runtime/subscriptions'], render: () => 'never'}];
"""
        + code
        + r"""
(async () => {
  await loadPanel(PANELS[0]);
  const html = document.getElementById('usage-content').innerHTML;
  const visible = html.split('<details>')[0];
  assert.match(visible, /This panel couldn't load: the source failed\./);
  assert.match(visible, /<button[^>]*>Retry<\/button>/);
  assert.doesNotMatch(visible, /\/api\/runtime\/subscriptions/);
  assert.match(html, /<details><summary>Technical detail<\/summary>/);
  assert.match(html, /\/api\/runtime\/subscriptions: HTTP 503/);
  const age = document.getElementById('usage-age');
  assert.match(age.textContent, /^Fetched at \d{2}:\d{2}:\d{2} UTC$/);
  assert.equal(age.className, 'age bad');
  assert.equal(document.getElementById('usage-status').textContent, "Subscriptions couldn't load.");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refresh_header_names_a_failure_and_claims_updated_only_after_success():
    """Defect class: the header says Updated when a panel refresh failed."""
    page = machinery_path("api/dashboards/index.html").read_text()
    script = page.split("<script>", 1)[1].split("\nrefreshAll();", 1)[0]
    _node(
        _DOM
        + r"""
const realSetTimeout = setTimeout;
setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url).includes('/api/runtime/subscriptions');
  return {ok: !refused, status: refused ? 503 : 200, text: async () => '{}', headers: {get: () => '2'}};
};
"""
        + script
        + r"""
(async () => {
  await refreshAll();
  const status = document.getElementById('last-refresh');
  const retry = document.getElementById('refresh-retry');
  assert.match(status.textContent, /^Couldn't refresh, last attempt \d{2}:\d{2}:\d{2} UTC$/);
  assert.equal(retry.hidden, false);
  assert.doesNotMatch(status.textContent, /Updated/);
  assert.match(document.getElementById('usage-content').innerHTML, /couldn't load/i);
  assert.doesNotMatch(document.getElementById('workers-content').innerHTML, /couldn't load/i);
  failSubscriptions = false;
  await refreshAll();
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(retry.hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /couldn't load/i);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_home_announces_panel_status_and_has_skip_purpose_and_sites_label():
    """Defect class: a panel status change is silent, or the skip link, sites label, or purpose line is missing."""
    page = machinery_path("api/dashboards/index.html").read_text()
    assert 'class="skip" href="#main"' in page
    assert '<main id="main" tabindex="-1">' in page
    assert 'aria-label="Sites"' in page
    assert page.count('aria-live="polite"') == 6
    assert "Which subscription is near its limit, when it resets, and whether anything is signed out." in page
    script = page.split("<script>", 1)[1].split("\nrefreshAll();", 1)[0]
    _node(
        r"""
const assert = require('node:assert/strict');
const announced = [];
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) {
    let text = '';
    const el = {innerHTML: '', className: '', hidden: true, setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}};
    Object.defineProperty(el, 'textContent', {
      get() { return text; },
      set(next) { text = String(next); if (id.endsWith('-status')) announced.push(text); },
    });
    elements.set(id, el);
  }
  return elements.get(id);
}};
const realSetTimeout = setTimeout;
setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url).includes('/api/runtime/subscriptions');
  return {ok: !refused, status: refused ? 403 : 200, text: async () => '{}', headers: {get: () => '2'}};
};
"""
        + script
        + r"""
(async () => {
  await refreshAll();
  assert.ok(announced.indexOf('Loading Subscriptions.') < announced.indexOf("Subscriptions isn't available in this view."));
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
  failSubscriptions = false;
  await refreshAll();
  assert.equal(announced.filter(item => item === 'Subscriptions loaded.').length, 1);
  await refreshAll();
  assert.equal(announced.filter(item => item === 'Subscriptions loaded.').length, 1);
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refused_panels_stay_unavailable_and_do_not_fail_refresh():
    """Defect class: a panel refused in this view turns the page into Couldn't refresh, or shows Retry and a red age."""
    page = machinery_path("api/dashboards/index.html").read_text()
    script = page.split("<script>", 1)[1].split("\nrefreshAll();", 1)[0]
    _node(
        _DOM
        + r"""
const realSetTimeout = setTimeout;
setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
const REFUSED = ['/api/delegate/active', '/api/delegate/recent', '/api/git/pull-requests'];
globalThis.fetch = async (url) => {
  const refused = REFUSED.some(item => String(url).includes(item));
  return {ok: !refused, status: refused ? 403 : 200, headers: {get: () => '2'}, text: async () => JSON.stringify({
    tasks: [], status: 'ok', alerts: [], heartbeat: true, providers: [], deliveries: {}, jobs: {},
    dead_letters: 0, store: {reachable: true}, repositories: []})};
};
"""
        + script
        + r"""
(async () => {
  await refreshAll();
  const status = document.getElementById('last-refresh');
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(document.getElementById('refresh-retry').hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  for (const id of ['workers', 'recent', 'prs']) {
    const html = document.getElementById(id + '-content').innerHTML;
    assert.match(html, /isn't available in this view/);
    assert.doesNotMatch(html, /Retry|couldn't load|class="error"/);
    const age = document.getElementById(id + '-age');
    assert.match(age.textContent, /^Fetched at \d{2}:\d{2}:\d{2} UTC$/);
    assert.equal(age.className, 'age');
  }
  assert.doesNotMatch(document.getElementById('alerts-content').innerHTML, /isn't available|couldn't load/);
  assert.match(document.getElementById('alerts-age').textContent, /Fetched at \d{2}:\d{2}:\d{2} UTC \u00b7 source age 2s/);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_at_risk_chip_counts_fresh_run_outs_and_names_unknowns():
    """Defect class: one account with no reading replaces the run-out count with a question mark."""
    code = _script("index.html", "function escapeHtml", "// ---- Panels ----")
    _node(
        _DOM
        + code
        + r"""
const fresh = {reading: {status: 'fresh', age_s: 30, read_at: '2026-11-13T00:00:00Z'},
  pace: {status: 'ok', lasts_to_reset: false}};
const unread = {reading: {status: 'unknown', age_s: null, reason_code: 'provider_error'}, pace: {status: 'unknown'}};
const safe = {reading: {status: 'fresh', age_s: 30, read_at: '2026-11-13T00:00:00Z'},
  pace: {status: 'ok', lasts_to_reset: true}};
const chipOf = (...accounts) => renderChips({usage: {stale_after_s: 900, providers: [{accounts}]}});
const mixed = chipOf(fresh, fresh, unread);
assert.match(mixed, /<b>2<\/b> accounts run out before reset, 1 unknown/);
assert.match(mixed, /class="chip bad"/);
assert.doesNotMatch(mixed, /\?/);
assert.match(chipOf(fresh), /<b>1<\/b> account runs out before reset/);
const none = chipOf(safe, unread);
assert.match(none, /<b>0<\/b> accounts run out before reset, 1 unknown/);
assert.match(none, /class="chip warn"/);
assert.doesNotMatch(none, /chip bad/);
const clear = chipOf(safe);
assert.match(clear, /<b>0<\/b> accounts run out before reset</);
assert.doesNotMatch(clear, /unknown|chip bad|chip warn/);
"""
    )
