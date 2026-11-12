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
  assert.equal(document.getElementById('fast-age').textContent, 'Source age 900s');
  assert.equal(document.getElementById('fast-age').className, 'age bad');
  assert.equal(loginPolls, 1, 'pending login retries independently of another failed panel');
  assert.match(document.getElementById('broken-content').innerHTML, /Unavailable: \/broken: HTTP 503/);
  assert.doesNotMatch(document.getElementById('broken-content').innerHTML, /old data/);
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
    code = _script("index.html", "function escapeHtml", "// ---- Summary chips ----")
    _node(_DOM + code + r"""
const row = {account: 'Fixture', reading: {status: 'fresh', age_s: 60, read_at: '2026-11-12T00:00:00Z'},
  weekly: {status: 'ok', left_pct: 70, resets_at: '2099-01-02T03:04:00Z'},
  pace: {status: 'ok', state: 'reserve', headroom_pct: 18, lasts_to_reset: true},
  session: {status: 'ok', left_pct: 61, resets_at: '2099-01-02T03:04:00Z'},
  extra_windows: [{label: 'Extra', status: 'ok', left_pct: 84}], breakdown: [{label: 'Used', value: '91 units'}],
  credits: {status: 'ok', balance: 37.2}, plan: {name: 'Fixture plan'}};
const render = reading => subscriptionRows({stale_after_s: 900,
  providers: [{provider: 'fixture', accounts: [{...row, reading}]}]}, null).groups;
assert.match(render(row.reading), /70%/);
for (const reading of [{...row.reading, age_s: 1800}, {...row.reading, status: 'stale'},
    {...row.reading, status: 'unknown'}, {...row.reading, age_s: null}]) {
  const html = render(reading);
  assert.doesNotMatch(html, /70%|61%|84%|91 units|37\.2|2099-01-02|18 pp|lasts to reset/);
  assert.match(html, /Fixture plan/);
}
const apiAccountRow = {freshness: 'fresh', age_s: 60, total_balance: 42, currency: 'USD', usage_weekly_usd: 9};
const api = reading => apiRows({api_accounts: {Fixture: {...apiAccountRow, ...reading}},
  diagnostics: {stale_threshold_s: 900}}).flat().join('');
assert.match(api({}), /42\.00 USD balance/);
assert.match(api({}), /9\.00 this week/);
for (const reading of [{age_s: 1800}, {freshness: 'stale'}, {freshness: 'stale_last_good'},
    {freshness: 'unknown'}, {age_s: null}]) assert.doesNotMatch(api(reading), /42\.00|9\.00/);
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
