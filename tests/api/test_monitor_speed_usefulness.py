"""Behavioral regressions for Monitor loading and honest queue evidence (#7684)."""

from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from lu_machinery.api import work_router
from lu_machinery.api.main import app
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration import fleet_repos


def _node(script: str) -> None:
    result = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _machinery_js_prelude() -> str:
    js_path = machinery_path("api/dashboards/machinery.js")
    return f"const Machinery = require({json.dumps(str(js_path))});\nglobalThis.Machinery = Machinery;\n"


def _page_script(page: str, *, until: str = "\nrunner.start();") -> str:
    html = (machinery_path("api/dashboards") / page).read_text()
    marker = '<script src="/machinery.js"></script>'
    after = html.split(marker, 1)[1]
    inline = after.split("<script>", 1)[1].split("</script>", 1)[0]
    return inline.split(until, 1)[0]


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
const document = {querySelectorAll() { return []; }, getElementById(id) {
  if (!elements.has(id)) elements.set(id, {textContent: 'old data', innerHTML: 'old data', className: '',
    setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}});
  return elements.get(id);
}};
const tick = () => new Promise(resolve => setImmediate(resolve));
"""


def test_home_panels_paint_independently_and_a_failed_panel_clears_old_data():
    _node(
        _DOM
        + _machinery_js_prelude()
        + r"""
const calls = [];
let loginPolls = 0;
const setTimeout = (fn, ms) => {if (ms === 3000) loginPolls++;};
globalThis.setTimeout = setTimeout;
let resolveSlow;
const slow = new Promise(resolve => {resolveSlow = resolve;});
const fakeFetch = async url => {
  calls.push(url);
  if (url === '/slow') return slow;
  if (url === '/broken') throw new Error('/broken: HTTP 503');
  if (url === '/login') return {data: {state: 'pending'}, age: null, snapshot: false};
  return {data: {tasks: []}, age: 900, bound: 150, snapshot: true};
};
const PANELS = [
  {id: 'fast', urls: ['/fast'], render: data => `fast ${data.tasks.length}`},
  {id: 'broken', urls: ['/broken'], render: () => 'never'},
  {id: 'slow', urls: ['/slow'], render: data => `slow ${data.marker}`},
  {id: 'logins', urls: ['/login'], render: data => data.state, retry: data => data.state === 'pending'},
];
const runner = Machinery.createPanelRunner({panels: PANELS, optionalUrls: new Set(), fetchJson: fakeFetch});
(async () => {
  const first = Promise.all(PANELS.map(runner.loadPanel));
  await tick();
  assert.equal(document.getElementById('fast-content').innerHTML, 'fast 0');
  assert.match(document.getElementById('fast-age').textContent, /^Fetched at \d{2}:\d{2}:\d{2} UTC · STALE · source age 900s · bound 150s$/);
  assert.equal(document.getElementById('fast-age').className, 'age bad');
  assert.equal(loginPolls, 1, 'pending login retries independently of another failed panel');
  const broken = document.getElementById('broken-content').innerHTML;
  assert.match(broken, /This panel couldn't load/);
  assert.match(broken, /\/broken: HTTP 503/);
  assert.doesNotMatch(broken.split('<details>')[0], /\/broken/);
  assert.doesNotMatch(broken, /old data/);
  assert.equal(document.getElementById('slow-content').innerHTML, 'old data');
  await runner.loadPanel(PANELS[2]);
  assert.equal(calls.filter(url => url === '/slow').length, 1, 'overlap must not duplicate requests');
  resolveSlow({data: {marker: 'new'}, age: null, snapshot: false});
  await first;
  assert.equal(document.getElementById('slow-content').innerHTML, 'slow new');
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    )
    fleet_script = _page_script("fleet.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + fleet_script + r"""
const recent = renderRecent({tasks: [{task_id: 'finished', status: 'done'}],
  needs_attention: {total: 1530, tasks: [{task_id: 'unfinished', status: 'needs_finalize'},
    {task_id: 'crashed', status: 'zombie'}]}});
assert.match(recent, /1530 unfinished task records need a keep\/close decision/);
assert.match(recent, /Showing up to 2/);
assert.match(recent, /needs_finalize/);
assert.match(recent, /zombie/);
assert.match(recent, /finished/);
assert.doesNotMatch(renderWorkers({tasks: []}), /unfinished|zombie/);
""")


def test_home_hides_stale_subscription_quota_and_api_balances():
    """Stale or unverified readings must not render actionable quota, pace, reset or balance numbers."""
    subs_script = _page_script("models.html", until="\nconst PANELS = [")
    overview_script = _page_script("index.html", until="\nconst PANELS = [")
    _node(
        _DOM
        + _machinery_js_prelude()
        + "(() => {\n"
        + subs_script
        + "\nglobalThis.subscriptionRows = subscriptionRows; globalThis.renderUsage = renderUsage; globalThis.apiRows = apiRows;\n})();\n"
        + "(() => {\n"
        + overview_script
        + "\nglobalThis.renderAlerts = renderAlerts; globalThis.renderChips = renderChips;\n})();\n"
        + r"""
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
assert.doesNotMatch(lastGood, /42\.00|9\.00/);
assert.match(lastGood, /unknown; needs an API key/);
assert.match(lastGood, /Last good reading, 1m old/);
assert.doesNotMatch(lastGood, /STALE_LAST_GOOD|NEED_KEY/);
assert.match(lastGood, /class="warn">Last good reading/);
const known = {title: 'Approved change not merged', meaning: 'Held over 30 minutes.', next_step: 'The owner lands it.'};
const pool = {title: 'Subscription pool will run out early', meaning: 'Ends before reset.', next_step: 'Work goes elsewhere.'};
const named = renderAlerts({status: 'ok', heartbeat: true, alerts: [
  {alertname: 'UnrecognizedAlert', severity: 'warning', starts_at: '2026-11-01T00:00:00Z'},
  {alertname: 'ApprovedNotLanded30m', severity: 'warning', starts_at: '2026-11-12T00:00:00Z', ...known},
  {alertname: 'ApprovedNotLanded30m', severity: 'warning', starts_at: '2026-11-11T00:00:00Z', ...known},
  {alertname: 'SubscriptionPoolRunsOutBeforeReset', severity: 'warning', subject: 'Codex b', ...pool},
  {alertname: 'SubscriptionPoolRunsOutBeforeReset', severity: 'warning', subject: 'AGY Claude/GPT', ...pool},
  {alertname: 'SubscriptionPoolRunsOutBeforeReset', severity: 'warning', subject: 'AGY Claude/GPT', ...pool},
  {alertname: 'CliHealthBehind', severity: 'warning', starts_at: '2026-11-02T00:00:00Z'},
  {alertname: 'CliHealthCheckFailed', severity: 'warning', subject: 'Claude', starts_at: '2026-11-06T00:00:00Z'},
  {alertname: 'CliHealthCheckFailed', severity: 'critical', subject: 'Claude', starts_at: '2026-11-05T00:00:00Z'},
  {alertname: 'CliHealthCheckFailed', severity: 'unknown', subject: 'Claude', starts_at: '2026-11-07T00:00:00Z'},
  {alertname: 'SubscriptionPoolRunsOutBeforeReset', severity: 'warning', subject: 'Claude b', starts_at: '2026-11-08T00:00:00Z'},
  {alertname: 'SomeFutureAlert', severity: 'critical', starts_at: '2026-11-03T00:00:00Z', title: null},
  {alertname: 'UsagePoolRestored', severity: 'info', subject: 'Grok+Cursor', starts_at: '2026-11-04T00:00:00Z',
   title: 'Pool allowance is back', meaning: 'Recovered.', next_step: 'No action needed.'}]});
const [problems, information] = named.split('<h3>Recent information</h3>');
assert.match(problems, /Unknown alert \(name not listed\)/);
assert.match(problems, /Unknown alert \(name not listed\): CliHealthBehind/);
assert.match(problems, /Critical: Unknown alert \(name not listed\): SomeFutureAlert/);
assert.doesNotMatch(named, /UnrecognizedAlert/);
assert.match(problems, /Approved change not merged \(2 firing\)/);
// Severity is per instance: one alert and subject is one row, at its highest severity and earliest start.
assert.equal((problems.match(/CliHealthCheckFailed|Unknown alert \(name not listed\): CliHealthCheckFailed/g) || []).length, 1);
assert.match(problems, /Critical: Unknown alert \(name not listed\): CliHealthCheckFailed: Claude \(3 firing\)/);
assert.match(problems, /2026-11-05 00:00Z/);
// A listed name without text keeps its subject.
assert.match(problems, /Unknown alert \(name not listed\): SubscriptionPoolRunsOutBeforeReset: Claude b/);
assert.match(problems, /Subscription pool will run out early: Codex b</);
assert.match(problems, /Subscription pool will run out early: AGY Claude\/GPT \(2 firing\)/);
assert.equal((problems.match(/Approved change not merged/g) || []).length, 1);
assert.equal((problems.match(/Subscription pool will run out early/g) || []).length, 2);
assert.match(problems, /2026-11-11 00:00Z \(/);
assert.match(problems, /What happens next/);
// Good news is shown apart, never as a problem.
assert.ok(information, 'information block');
assert.match(information, /Pool allowance is back: Grok\+Cursor/);
assert.doesNotMatch(problems, /Pool allowance is back/);
assert.match(renderAlerts({status: 'ok', heartbeat: true, alerts: []}), /Nothing firing\./);
const chipFor = alerts => renderChips({alerts: {status: 'ok', alerts}});
assert.match(chipFor([{alertname: 'UsagePoolRestored', severity: 'info'}]), /<b>0<\/b> alerts firing/);
assert.match(chipFor([{alertname: 'A', severity: 'info'}, {alertname: 'B', severity: 'critical'}]), /<b>1<\/b> alerts firing/);
assert.match(renderAlerts({status: 'ok', heartbeat: true, alerts: [
  {alertname: 'UsagePoolRestored', severity: 'info', title: 'Pool allowance is back'}]}), /No problems firing\./);
""")
    models_full_script = _page_script("models.html")
    _node(
        _DOM
        + _machinery_js_prelude()
        + r"""
globalThis.document = document;
const realPerfNow = performance.now.bind(performance);
const realDateNow = Date.now;
let monoNow = 10000;
let fakeNow = Date.parse('2026-11-28T01:00:00Z');
performance.now = () => monoNow;
Date.now = () => fakeNow;
let budgetFetches = 0;
let poolMode = 'all_fresh';
let apiFetchedAt = '2026-11-28T00:45:01Z';
let generatedAt = '2026-11-28T01:00:00Z';
let cachedDataAgeS = 0;
let cachedApiAgeS = 899;
let subResetAt = '2099-01-02T03:04:00Z';
globalThis.fetch = async (url) => {
  const s = String(url);
  if (s.startsWith('/api/state/routing-budget')) {
    budgetFetches += 1;
    const agents = poolMode === 'all_fresh'
      ? {
          claude: {freshness: 'fresh', age_s: 899, status: 'cool', stale: false},
          cursor: {freshness: 'fresh', age_s: 899, status: 'cool', stale: false, provider_windows: {auto: {remaining_pct: 82}, api: {remaining_pct: 64}}},
        }
      : poolMode === 'mixed'
      ? {
          claude: {freshness: 'fresh', age_s: 60, status: 'cool', stale: false},
          codex: {freshness: 'stale', age_s: 1200, status: 'cool', stale: true},
        }
      : poolMode === 'need_login_only'
      ? {
          cursor: {freshness: 'fresh', age_s: 30, status: 'need_login', login_state: 'NEED_LOGIN', probe_state: 'NEED_LOGIN', stale: false},
        }
      : poolMode === 'need_login_mixed'
      ? {
          claude: {freshness: 'fresh', age_s: 30, status: 'cool', stale: false},
          cursor: {freshness: 'fresh', age_s: 30, status: 'need_login', login_state: 'NEED_LOGIN', probe_state: 'NEED_LOGIN', stale: false},
        }
      : poolMode === 'need_probe_hot_only'
      ? {
          cursor: {
            freshness: 'fresh', age_s: 30, status: 'hot', stale: false,
            login_state: 'authenticated', probe_state: 'NEED_PROBE',
            provider_windows: {auto: {used_pct: null, remaining_pct: null}, api: {used_pct: null, remaining_pct: null}},
            runtime: {headroom_blocked: true},
          },
        }
      : poolMode === 'need_probe_hot_mixed'
      ? {
          claude: {freshness: 'fresh', age_s: 30, status: 'cool', stale: false, remaining_pct: 70},
          cursor: {
            freshness: 'fresh', age_s: 30, status: 'hot', stale: false,
            login_state: 'authenticated', probe_state: 'NEED_PROBE',
            provider_windows: {auto: {used_pct: null, remaining_pct: null}, api: {used_pct: null, remaining_pct: null}},
            runtime: {headroom_blocked: true},
          },
        }
      : poolMode === 'shared_grok_cursor_near_cap'
      ? {
          grok: {freshness: 'fresh', age_s: 30, status: 'near_cap', stale: false, remaining_pct: 0, pool_pace: {pool: 'grok', status: 'near_cap', stale: false, age_s: 30}},
          cursor: {freshness: 'fresh', age_s: 30, status: 'near_cap', stale: false, probe_state: 'NEED_PROBE', provider_windows: {auto: {used_pct: null, remaining_pct: null}, api: {used_pct: null, remaining_pct: null}}, pool_pace: {pool: 'grok', status: 'near_cap', stale: false, age_s: 30}},
        }
      : poolMode === 'stale_pool_pace_fresh_cursor'
      ? {
          cursor: {
            freshness: 'fresh', age_s: 30, status: 'near_cap', lane_status: 'cool', stale: false,
            login_state: 'authenticated', probe_state: 'healthy', remaining_pct: 70,
            provider_windows: {auto: {used_pct: 30, remaining_pct: 70}, api: {used_pct: 20, remaining_pct: 80}},
            pool_pace: {pool: 'grok', status: 'near_cap', stale: true, age_s: 901},
          },
        }
      : poolMode === 'expiring_pool_pace_only'
      ? {
          cursor: {
            freshness: 'fresh', age_s: 30, status: 'near_cap', lane_status: 'unknown', stale: false,
            login_state: 'authenticated', probe_state: 'NEED_PROBE',
            provider_windows: {auto: {used_pct: null, remaining_pct: null}, api: {used_pct: null, remaining_pct: null}},
            pool_pace: {pool: 'grok', status: 'near_cap', stale: false, age_s: 899},
          },
        }
      : poolMode === 'seven_canonical_pools'
      ? {
          claude: {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 73},
          codex: {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 80},
          gemini: {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 85},
          grok: {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 90},
          cursor: {freshness: 'fresh', age_s: 30, stale_threshold_s: 600, status: 'cool', stale: false, provider_windows: {auto: {remaining_pct: 90}, api: {remaining_pct: 88}}},
        }
      : poolMode === 'source_ttl_expiry'
      ? {
          claude: {
            freshness: 'fresh', age_s: 809, stale_threshold_s: 810, status: 'near_cap', lane_status: 'cool', stale: false, remaining_pct: 73,
            pool_pace: {pool: 'claude:a', status: 'near_cap', stale: false, age_s: 809, stale_threshold_s: 810},
          },
          codex: {freshness: 'fresh', age_s: 809, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 75},
          cursor: {freshness: 'fresh', age_s: 599, stale_threshold_s: 600, status: 'cool', stale: false, provider_windows: {auto: {remaining_pct: 82}, api: {remaining_pct: 64}}},
        }
      : {
          claude: {freshness: 'fresh', age_s: 599, status: 'cool', stale: false},
        };
    const codexAccounts = poolMode === 'seven_canonical_pools'
      ? {
          'codex:a': {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 80},
          'codex:b': {freshness: 'fresh', age_s: 30, stale_threshold_s: 810, status: 'cool', stale: false, remaining_pct: 70},
        }
      : undefined;
    const isolatedCursorModes = new Set([
      'need_login_only', 'need_probe_hot_only', 'shared_grok_cursor_near_cap',
      'stale_pool_pace_fresh_cursor', 'expiring_pool_pace_only',
    ]);
    const subProviders = isolatedCursorModes.has(poolMode)
      ? []
      : poolMode === 'source_ttl_expiry'
      ? [
          {
            provider: 'claude',
            label: 'Claude',
            accounts: [
              {
                account: 'Claude a',
                source: 'monitor',
                pool_status: 'near_cap',
                reading: {status: 'fresh', age_s: 809, stale_threshold_s: 810, read_at: '2026-11-28T00:55:31Z'},
                weekly: {status: 'ok', used_pct: 100, left_pct: 0, resets_at: subResetAt},
                pace: {status: 'ok', state: 'deficit', headroom_pct: -20, lasts_to_reset: false, runs_out_in_s: 0},
                plan: {status: 'ok'},
              },
            ],
          },
        ]
      : poolMode === 'seven_canonical_pools'
      ? [
          {
            provider: 'claude',
            label: 'Claude',
            accounts: [
              {
                account: 'Claude a',
                source: 'monitor',
                pool_status: 'cool',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 810, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 27, left_pct: 73, resets_at: subResetAt},
                pace: {status: 'ok', state: 'reserve', headroom_pct: 14, lasts_to_reset: true},
                plan: {status: 'ok'},
              },
              {
                account: 'Claude b',
                source: 'monitor',
                pool_status: 'near_cap',
                reading: {status: 'fresh', age_s: 809, stale_threshold_s: 810, read_at: '2026-11-28T00:46:31Z'},
                weekly: {status: 'ok', used_pct: 100, left_pct: 0, resets_at: subResetAt},
                pace: {status: 'ok', state: 'deficit', headroom_pct: -30, lasts_to_reset: false, runs_out_in_s: 0},
                plan: {status: 'ok'},
              },
            ],
          },
          {
            provider: 'codex',
            label: 'Codex',
            accounts: [
              {
                account: 'Codex a',
                source: 'usage_cache',
                pool_status: 'cool',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 900, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 99.0, left_pct: 1.0, resets_at: subResetAt},
                pace: {status: 'ok', state: 'on_pace', headroom_pct: 0, lasts_to_reset: false},
                plan: {status: 'ok'},
              },
              {
                account: 'Codex b',
                source: 'usage_cache',
                pool_status: 'cool',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 900, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 30, left_pct: 70, resets_at: subResetAt},
                pace: {status: 'ok', state: 'on_pace', headroom_pct: 0, lasts_to_reset: true},
                plan: {status: 'ok'},
              },
            ],
          },
          {
            provider: 'agy',
            label: 'AGY',
            accounts: [
              {
                account: 'AGY Gemini',
                source: 'usage_cache',
                pool_status: 'cool',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 900, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 15, left_pct: 85, resets_at: subResetAt},
                pace: {status: 'ok', state: 'reserve', headroom_pct: 10, lasts_to_reset: true},
                plan: {status: 'ok'},
              },
              {
                account: 'AGY Claude/GPT',
                source: 'usage_cache',
                pool_status: 'near_cap',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 900, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 99.5, left_pct: 0.5, resets_at: subResetAt},
                pace: {status: 'ok', state: 'deficit', headroom_pct: -20, lasts_to_reset: false, runs_out_in_s: 0},
                plan: {status: 'ok'},
              },
            ],
          },
          {
            provider: 'grok',
            label: 'Grok',
            accounts: [
              {
                account: 'Grok+Cursor',
                source: 'usage_cache',
                pool_status: 'cool',
                reading: {status: 'fresh', age_s: 30, stale_threshold_s: 900, read_at: '2026-11-28T01:00:00Z'},
                weekly: {status: 'ok', used_pct: 10, left_pct: 90, resets_at: subResetAt},
                pace: {status: 'ok', state: 'reserve', headroom_pct: 20, lasts_to_reset: true},
                plan: {status: 'ok'},
              },
            ],
          },
        ]
      : [{
          provider: 'claude',
          label: 'Claude',
          accounts: [{
            account: 'Claude a',
            pool_status: 'cool',
            reading: {status: 'fresh', age_s: 899, read_at: '2026-11-28T00:45:01Z'},
            weekly: {status: 'ok', left_pct: 73, resets_at: subResetAt},
            pace: {status: 'ok', state: 'reserve', headroom_pct: 14, lasts_to_reset: true},
            plan: {status: 'ok'},
          }],
        }];
    const apiAccounts = poolMode === 'source_ttl_expiry'
      ? {FixtureAPI: {freshness: 'fresh', age_s: 599, stale_threshold_s: 600, fetched_at: apiFetchedAt, total_balance: 55, currency: 'USD', usage_weekly_usd: 12}}
      : {FixtureAPI: {freshness: 'fresh', age_s: cachedApiAgeS, fetched_at: apiFetchedAt, total_balance: 55, currency: 'USD', usage_weekly_usd: 12}};
    return {
      ok: true,
      status: 200,
      headers: {get: () => null},
      text: async () => JSON.stringify({
        generated_at: generatedAt,
        diagnostics: {stale_threshold_s: 900, data_age_s: cachedDataAgeS, codexbar_data_available: true, codexbar_max_age_s: 0},
        agents,
        ...(codexAccounts ? {codex_accounts: codexAccounts} : {}),
        api_accounts: apiAccounts,
        subscriptions: {
          stale_after_s: 900,
          providers: subProviders,
        },
      }),
    };
  }
  if (s === '/api/runtime/logins') {
    if (poolMode === 'mixed') return {ok: false, status: 503, headers: {get: () => null}, text: async () => '{}'};
    return {ok: true, status: 200, headers: {get: () => null}, text: async () => JSON.stringify({state: 'ok', accounts: [{account: 'Claude a', logged_in: true}]})};
  }
  return {ok: true, status: 200, headers: {get: () => null}, text: async () => '{}'};
};
const models = (() => {
"""
        + models_full_script
        + r"""
  return {runner, PANELS, renderChips};
})();
(async () => {
  try {
    await models.runner.loadPanel(models.PANELS[0]);
    const updateChips = () => { document.getElementById('chips').innerHTML = models.renderChips(models.runner.latest); };
    updateChips();
    assert.equal(budgetFetches, 1);
    const freshHtml = document.getElementById('usage-content').innerHTML;
    assert.match(freshHtml, /73%/);
    assert.match(freshHtml, /82% left/);
    assert.match(freshHtml, /55\.00 USD balance/);
    const freshChips = document.getElementById('chips').innerHTML;
    assert.match(freshChips, /<b>73%<\/b> Claude/);
    assert.match(freshChips, /<b>0<\/b> pools near cap/);
    assert.doesNotMatch(freshChips, /unknown/);

    // Wall-clock step alone does not expire the monotonic budget clock.
    fakeNow += 5000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.equal(budgetFetches, 1);
    assert.match(document.getElementById('usage-content').innerHTML, /73%/);

    // Monotonic clock advancing 2s crosses the 900s threshold and withdraws all expired figures + chips with zero fetches.
    monoNow += 2000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.equal(budgetFetches, 1, 'F3: budget expiry makes zero extra fetches');
    const expiredHtml = document.getElementById('usage-content').innerHTML;
    assert.doesNotMatch(expiredHtml, /73%|82% left|64% left|55\.00|12\.00/);
    const expiredChips = document.getElementById('chips').innerHTML;
    assert.match(expiredChips, /<b>\?<\/b> Claude/);
    assert.match(expiredChips, /<b>\?<\/b> pools near cap unknown/);
    assert.doesNotMatch(expiredChips, /<b>0<\/b> pools near cap/);

    // Rolling monotonic clock back never revives expired figures.
    monoNow -= 5000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /73%|55\.00/);

    // F3 cached-poll revival guard: a 599s API reading expires after 306s and stays hidden when
    // the next poll returns the same cached generation (same generated_at/fetched_at, same age_s=599).
    monoNow = 20000;
    poolMode = 'cached_api';
    generatedAt = '2026-11-28T01:05:00Z';
    apiFetchedAt = '2026-11-28T00:55:01Z';
    cachedApiAgeS = 599;
    cachedDataAgeS = 0;
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('usage-content').innerHTML, /55\.00 USD balance/);
    monoNow += 306000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /55\.00|12\.00/);
    cachedDataAgeS = 306;
    cachedApiAgeS = 905;
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /55\.00|12\.00/, 'F3: cached poll must not revive expired API balance');
    // Server-aged cached snapshot hides expired balance immediately on first paint.
    generatedAt = '2026-11-28T01:10:00Z';
    apiFetchedAt = '2026-11-28T01:00:01Z';
    cachedApiAgeS = 905;
    cachedDataAgeS = 306;
    await models.runner.loadPanel(models.PANELS[0]);
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /55\.00|12\.00/, 'F3: pre-aged cached server snapshot hides expired balance immediately');
    // F3 cross-generation guard: switching generated_at while diagnostics jumps from 1s ledger to 401s CodexBar
    // must NOT over-age a 550s -> 582s API reading past 900s.
    monoNow = 400000;
    generatedAt = '2026-11-28T01:11:00Z';
    apiFetchedAt = '2026-11-28T01:01:50Z';
    cachedApiAgeS = 550;
    cachedDataAgeS = 1;
    await models.runner.loadPanel(models.PANELS[0]);
    assert.match(document.getElementById('usage-content').innerHTML, /55\.00 USD balance/);
    monoNow += 32000;
    generatedAt = '2026-11-28T01:11:32Z';
    cachedApiAgeS = 582;
    cachedDataAgeS = 401;
    await models.runner.loadPanel(models.PANELS[0]);
    assert.match(document.getElementById('usage-content').innerHTML, /55\.00 USD balance/, 'F3: cross-generation diag jump must not suppress fresh API balance');
    // #1574 Unix-epoch reset guard carried onto models.html.
    subResetAt = '1970-01-01T00:00:00Z';
    await models.runner.loadPanel(models.PANELS[0]);
    assert.match(document.getElementById('usage-content').innerHTML, /reset not reported/);
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /1970-01-01/);
    subResetAt = '2099-01-02T03:04:00Z';

    // F7: Mixed 1 fresh cool pool + 1 stale pool qualifies chip as "0 pools near cap (1 unknown)" with warn tone,
    // missing sign-in renders "? sign-in unknown", fresh NEED_LOGIN and authenticated NEED_PROBE with empty
    // windows + runtime hot count as unknown coverage, and shared Grok+Cursor pool counts once.
    cachedDataAgeS = 0;
    cachedApiAgeS = 60;
    apiFetchedAt = '2026-11-28T01:14:00Z';
    generatedAt = '2026-11-28T01:15:00Z';
    poolMode = 'mixed';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    const mixedChips = document.getElementById('chips').innerHTML;
    assert.match(mixedChips, /chip warn[^>]*><b>0<\/b> pools near cap \(1 unknown\)/);
    assert.match(mixedChips, /chip warn[^>]*><b>\?<\/b> sign-in unknown/);

    poolMode = 'need_login_only';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>\?<\/b> pools near cap unknown/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>0<\/b> pools near cap/);

    poolMode = 'need_login_mixed';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>0<\/b> pools near cap \(1 unknown\)/);

    poolMode = 'need_probe_hot_only';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>\?<\/b> pools near cap unknown/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>0<\/b> pools near cap/);

    poolMode = 'need_probe_hot_mixed';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>0<\/b> pools near cap \(1 unknown\)/);

    poolMode = 'shared_grok_cursor_near_cap';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>1<\/b> pool near cap<\/a>/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>2<\/b> pools near cap/);

    // PR1569-F7: Stale Grok+Cursor pool_pace copied onto a fresh 70%-remaining Cursor lane must NOT show 1 pool near cap.
    poolMode = 'stale_pool_pace_fresh_cursor';
    generatedAt = '2026-11-28T01:16:00Z';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /<b>0<\/b> pools near cap<\/a>/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>1<\/b> pool near cap/);

    // PR1569-F7: Fresh pool_pace at 899s on NEED_PROBE Cursor expires to unknown after +2s monotonic tick.
    poolMode = 'expiring_pool_pace_only';
    monoNow = 500000;
    generatedAt = '2026-11-28T01:17:00Z';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /<b>1<\/b> pool near cap<\/a>/);
    monoNow += 2000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.match(document.getElementById('chips').innerHTML, /<b>\?<\/b> pools near cap unknown/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>1<\/b> pool near cap/);

    // PR1569-F7: Canonical subscription pools without routing lanes (Claude b, AGY Claude/GPT) enter the near-cap chip,
    // using canonical backend pool_status (so rounded 99.0% Codex a with pool_status='cool' is not miscounted as near_cap).
    poolMode = 'seven_canonical_pools';
    monoNow = 550000;
    generatedAt = '2026-11-28T01:18:00Z';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>2<\/b> pools near cap<\/a>/);
    assert.doesNotMatch(document.getElementById('chips').innerHTML, /<b>0<\/b> pools near cap|<b>3<\/b> pools near cap/);
    // F1: When Claude b crosses its 810s Claude probe TTL on a browser tick, chip drops it to unknown while keeping AGY Claude/GPT near cap.
    monoNow += 2000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.match(document.getElementById('chips').innerHTML, /chip warn[^>]*><b>1<\/b> pool near cap \(1 unknown\)<\/a>/);

    // F1: Browser 1s ticks enforce each observation's source freshness limit (600s API/Cursor, 810s CodexBar & Claude sub/pool_pace)
    // even while the aggregate snapshot remains under 900s.
    poolMode = 'source_ttl_expiry';
    monoNow = 600000;
    generatedAt = '2026-11-28T01:19:00Z';
    apiFetchedAt = '2026-11-28T01:09:01Z';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('usage-content').innerHTML, /55\.00 USD balance/);
    assert.match(document.getElementById('usage-content').innerHTML, /82% left/);
    assert.match(document.getElementById('usage-content').innerHTML, /64% left/);
    assert.match(document.getElementById('chips').innerHTML, /<b>1<\/b> pool near cap<\/a>/);
    monoNow += 2000;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    const sourceExpiredHtml = document.getElementById('usage-content').innerHTML;
    assert.doesNotMatch(sourceExpiredHtml, /55\.00|12\.00|82% left|64% left/, 'F1: API & Cursor figures withdraw at 600s source limit');
    assert.match(sourceExpiredHtml, /STALE/, 'F1: Claude subscription Reading cell shows STALE at 810s probe TTL');
    assert.match(document.getElementById('chips').innerHTML, /<b>\?<\/b> Claude/, 'F1: Claude provider chip withdraws at 810s probe TTL');
    assert.match(document.getElementById('chips').innerHTML, /<b>\?<\/b> pools near cap unknown/, 'F1: Cursor (600s), Codex (810s), and Claude sub+pool_pace (810s) all withdraw at their source limits');

    // Non-finite monotonic clock withdraws figures as unknown rather than keeping stale fresh values.
    poolMode = 'all_fresh';
    cachedApiAgeS = 899;
    apiFetchedAt = '2026-11-28T01:20:00Z';
    generatedAt = '2026-11-28T01:20:00Z';
    await models.runner.loadPanel(models.PANELS[0]);
    updateChips();
    assert.match(document.getElementById('usage-content').innerHTML, /73%/);
    assert.match(document.getElementById('usage-content').innerHTML, /55\.00 USD balance/);
    performance.now = () => Number.NaN;
    models.PANELS[0].onTick(models.PANELS[0], models.runner.latest, updateChips, fakeNow);
    assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /73%|82% left|55\.00/);
    assert.match(document.getElementById('chips').innerHTML, /<b>\?<\/b> pools near cap unknown/);
  } finally {
    performance.now = realPerfNow;
    Date.now = realDateNow;
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    )


def test_home_comms_collection_errors_are_unavailable_even_when_store_exists():
    """A collector failure must not appear as successful plane access just because the database exists."""
    comms_script = _page_script("comms.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + comms_script + r"""
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
    _node(
        _DOM
        + "globalThis.fetch = async () => ({ok: true, text: async () => "
        + json.dumps(body)
        + "});\n"
        + _machinery_js_prelude()
        + r"""
assert.rejects(Machinery.fetchJson('/fixture'), /Invalid/).catch(e => {console.error(e); process.exitCode = 1;});
"""
    )


def test_failing_panel_shows_a_calm_sentence_retry_and_hidden_detail():
    """Defect class: a failed panel shows a raw API path, omits Retry, or leaves its age blank."""
    _node(
        _DOM
        + _machinery_js_prelude()
        + r"""
const fakeFetch = async url => { throw new Error(`${url}: HTTP 503`); };
const PANELS = [{id: 'usage', label: 'Subscriptions', urls: ['/api/runtime/subscriptions'], render: () => 'never'}];
const runner = Machinery.createPanelRunner({panels: PANELS, optionalUrls: new Set(), fetchJson: fakeFetch});
(async () => {
  await runner.loadPanel(PANELS[0]);
  const html = document.getElementById('usage-content').innerHTML;
  const visible = html.split('<details>')[0];
  assert.match(visible, /This panel couldn't load: the source failed\./);
  assert.match(visible, /<button[^>]*>Retry<\/button>/);
  assert.doesNotMatch(visible, /\/api\/runtime\/subscriptions/);
  assert.match(html, /<details><summary>Technical detail<\/summary>/);
  assert.match(html, /\/api\/runtime\/subscriptions: HTTP 503/);
  const age = document.getElementById('usage-age');
  assert.match(age.textContent, /^Last attempt at \d{2}:\d{2}:\d{2} UTC · unavailable · source age unknown$/);
  assert.equal(age.className, 'age bad');
  assert.equal(document.getElementById('usage-status').textContent, "Subscriptions couldn't load.");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refresh_header_names_a_failure_and_claims_updated_only_after_success(monkeypatch):
    """Defect class: the header says Updated when a panel refresh failed, or hides the running lu-machinery release."""
    health = TestClient(app).get("/api/health")
    assert health.status_code == 200
    version = health.json()["machinery_version"]
    assert version == importlib.metadata.version("lu-machinery")
    machinery = fleet_repos.FleetRepo("machinery", "new-owner/machinery", "machinery", "public-machinery")
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda path=None: {"machinery": machinery})
    served = TestClient(app).get("/machinery")
    assert served.status_code == 200
    data = re.search(r'id="machinery-version" data-version="([^"]*)" data-repository="([^"]*)"', served.text)
    assert data is not None
    assert data.groups() == (version, "new-owner/machinery")
    dataset = dict(zip(("version", "repository"), data.groups(), strict=True))
    assert "__MACHINERY_VERSION__" not in served.text
    script = _page_script("index.html")
    _node(
        _DOM
        + f"const dataset = {json.dumps(dataset)};\nconst VERSION = dataset.version;\n"
        + r"""
globalThis.document = document;
document.getElementById('machinery-version').dataset = dataset;
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url).includes('/api/runtime/subscriptions');
  if (String(url) === '/api/health') throw new Error('the header must not request /api/health');
  const body = '{}';
  return {ok: !refused, status: refused ? 503 : 200, text: async () => body, headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}};
};
"""
        + _machinery_js_prelude()
        + script
        + r"""
(async () => {
  await runner.refreshAll();
  assert.equal(document.getElementById('machinery-version').innerHTML,
    `<a href="https://github.com/new-owner/machinery/blob/main/CHANGELOG.md#v${VERSION}" title="What changed in this release">lu-machinery ${VERSION}</a>`);
  const status = document.getElementById('last-refresh');
  const retry = document.getElementById('refresh-retry');
  assert.match(status.textContent, /^Couldn't refresh, last attempt \d{2}:\d{2}:\d{2} UTC$/);
  assert.equal(retry.hidden, false);
  assert.doesNotMatch(status.textContent, /Updated/);
  assert.match(document.getElementById('usage-content').innerHTML, /couldn't load/i);
  assert.doesNotMatch(document.getElementById('alerts-content').innerHTML, /couldn't load/i);
  failSubscriptions = false;
  await runner.refreshAll();
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(retry.hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /couldn't load/i);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_home_announces_panel_status_and_has_skip_purpose_and_sites_label():
    """Defect class: a panel status change is silent, or the skip link, sites label, or purpose line is missing."""
    dashboards = machinery_path("api/dashboards")
    for name in ("index.html", "fleet.html", "comms.html", "pull-requests.html", "milestones.html", "models.html", "journal.html"):
        page = (dashboards / name).read_text()
        assert 'class="skip" href="#main"' in page, name
        assert '<main id="main" tabindex="-1">' in page, name
        assert 'aria-label="Sites"' in page, name
    assert (dashboards / "index.html").read_text().count('aria-live="polite"') == 4
    assert (dashboards / "fleet.html").read_text().count('aria-live="polite"') == 3
    assert (dashboards / "comms.html").read_text().count('aria-live="polite"') == 2
    models_page = (dashboards / "models.html").read_text()
    assert models_page.count('aria-live="polite"') == 4
    assert "What can be spent now, where recent work was routed, and which model gives the best value." in models_page
    assert (dashboards / "pull-requests.html").read_text().count('aria-live="polite"') == 1
    assert (dashboards / "milestones.html").read_text().count('aria-live="polite"') == 1
    assert (dashboards / "journal.html").read_text().count('aria-live="polite"') == 1

    models_script = _page_script("models.html")
    fleet_script = _page_script("fleet.html")
    _node(
        r"""
const assert = require('node:assert/strict');
const announced = [];
const elements = new Map();
const document = {querySelectorAll() { return []; }, getElementById(id) {
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
globalThis.document = document;
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url) === '/api/state/routing-budget?include_subscriptions=true';
  return {ok: !refused, status: refused ? 403 : 200, text: async () => '{}', headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}};
};
"""
        + _machinery_js_prelude()
        + "const modelsRunner = (() => {\n" + models_script + "\nreturn runner;\n})();\n"
        + "const fleetRunner = (() => {\n" + fleet_script + "\nreturn runner;\n})();\n"
        + r"""
(async () => {
  await modelsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.ok(announced.indexOf('Loading Budget now.') < announced.indexOf("Budget now isn't available in this view."));
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
  failSubscriptions = false;
  await modelsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.equal(announced.filter(item => item === 'Budget now loaded.').length, 1);
  await modelsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.equal(announced.filter(item => item === 'Budget now loaded.').length, 1);
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refused_panels_stay_unavailable_and_do_not_fail_refresh():
    """Defect class: a panel refused in this view turns the page into Couldn't refresh, or shows Retry and a red age."""
    overview_script = _page_script("index.html")
    fleet_script = _page_script("fleet.html")
    _node(
        _DOM
        + r"""
globalThis.document = document;
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
const REFUSED = ['/api/delegate/active', '/api/delegate/recent', '/api/git/pull-requests'];
globalThis.fetch = async (url) => {
  const refused = REFUSED.some(item => String(url).includes(item));
  return {ok: !refused, status: refused ? 403 : 200, headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}, text: async () => JSON.stringify({
    tasks: [], status: 'ok', alerts: [], heartbeat: true, providers: [], deliveries: {}, jobs: {},
    dead_letters: 0, store: {reachable: true}, repositories: []})};
};
"""
        + _machinery_js_prelude()
        + "const overviewRunner = (() => {\n" + overview_script + "\nreturn runner;\n})();\n"
        + "const fleetRunner = (() => {\n" + fleet_script + "\nreturn runner;\n})();\n"
        + r"""
(async () => {
  await overviewRunner.refreshAll();
  const status = document.getElementById('last-refresh');
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(document.getElementById('refresh-retry').hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  await fleetRunner.refreshAll();
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(document.getElementById('refresh-retry').hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  for (const id of ['workers', 'recent', 'prs']) {
    const html = document.getElementById(id + '-content').innerHTML;
    assert.match(html, /isn't available in this view/);
    assert.doesNotMatch(html, /Retry|couldn't load|class="error"/);
    const age = document.getElementById(id + '-age');
    assert.match(age.textContent, /^Last attempt at \d{2}:\d{2}:\d{2} UTC · unavailable · source age unknown$/);
    assert.equal(age.className, 'age');
  }
  assert.doesNotMatch(document.getElementById('alerts-content').innerHTML, /isn't available|couldn't load/);
  assert.match(document.getElementById('alerts-age').textContent, /Fetched at \d{2}:\d{2}:\d{2} UTC \u00b7 fresh \u00b7 source age \d+s \u00b7 bound 60s/);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_at_risk_chip_counts_fresh_run_outs_and_names_unknowns():
    """Defect class: one account with no reading replaces the run-out count with a question mark."""
    overview_script = _page_script("index.html", until="\nconst PANELS = [")
    _node(
        _DOM
        + _machinery_js_prelude()
        + overview_script
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


def test_panel_age_is_the_snapshot_header_plus_elapsed():
    """Defect class: client-side age from observed_at, or Number('') becoming 0, disagrees with the server snapshot age."""
    subs_script = _page_script("models.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + subs_script + r"""
const {headingAge, panelAgeSeconds} = Machinery;
const now = Date.now();
const skewed = new Date(now + 120000).toISOString();
const line = (...readings) => headingAge(readings.map(reading => ({receivedAt: now, snapshot: true, ...reading})), false, null, undefined, now);
// A present header wins over a body age and a future observed_at.
assert.match(line({url: '/api/milestones', age: 40, bound: 420, state: 'fresh',
  data: {status: 'ok', age_seconds: 817, stale: true, observed_at: skewed}}).text, /· fresh · source age 40s · bound 420s$/);
assert.equal(panelAgeSeconds({ageHeader: '200', receivedAt: now - 11000, data: {observed_at: skewed}}, now), 211);
assert.equal(panelAgeSeconds({ageHeader: '30', receivedAt: now + 5000}, now), 30);
assert.equal(panelAgeSeconds({ageHeader: '30', receivedAt: now}, Number.NaN), null);
// Missing, empty, whitespace, and non-decimal headers are unknown, never 0.
for (const raw of [undefined, null, '', ' ', '\t', 'unknown', '-1', '1e2', '0x10', '+5']) {
  const reading = {ageHeader: raw, boundHeader: '210', state: 'fresh', receivedAt: now,
    data: {status: 'ok', observed_at: skewed, age_seconds: 12, stale: true}};
  assert.equal(panelAgeSeconds(reading, now), null, JSON.stringify(raw));
  assert.match(line(reading).text, /source age unknown/);
  assert.doesNotMatch(line(reading).text, /source age 0s/);
}
// A real zero stays fresh under a positive bound. Age past the bound is stale.
assert.match(line({ageHeader: '0', boundHeader: '60', state: 'fresh', receivedAt: now, data: {status: 'ok'}}).text,
  /· fresh · source age 0s · bound 60s$/);
assert.match(line({ageHeader: '0.000', boundHeader: '60.000', state: 'fresh', receivedAt: now, data: {status: 'ok'}}).text,
  /· fresh · source age 0s · bound 60s$/);
assert.match(line({age: 240, bound: 210, state: 'fresh', receivedAt: now, data: {status: 'ok', observed_at: skewed}}).text,
  /· STALE · source age 240s · bound 210s$/);
assert.equal(line({age: 240, bound: 210, state: 'fresh', receivedAt: now, data: {status: 'ok'}}).bad, true);
// Server state stale (partial coverage) keeps the heading stale at the header age, without observed_at.
assert.match(line({age: 30, bound: 210, state: 'stale', receivedAt: now, url: '/api/git/pull-requests',
  data: {status: 'ok', observed_at: skewed, repositories: [{status: 'ok'}, {status: 'unknown'}]}}).text,
  /· STALE · source age 30s · bound 210s$/);
assert.match(line({ageHeader: '30', boundHeader: ' 210 ', state: ' STALE ', receivedAt: now, url: '/api/git/pull-requests?limit=1',
  data: {status: 'ok', observed_at: skewed, repositories: [{status: 'ok'}]}}).text,
  /· STALE · source age 30s · bound 210s$/);
assert.match(line({ageHeader: '5', boundHeader: '210', state: ' Unavailable ', receivedAt: now, url: '/api/git/pull-requests',
  data: {status: 'ok', repositories: [{status: 'ok'}]}}).text, /· unavailable · source age 5s/);
// No header at all is unknown, including when the body carries its own age.
assert.match(line({url: '/api/delegate/active', data: {tasks: []}, age: null, bound: null, state: null}).text,
  /· freshness unknown · source age unknown · bound unknown$/);
assert.match(line({url: '/api/milestones', data: {status: 'ok', age_seconds: 12, stale: false, observed_at: skewed}}).text,
  /· freshness unknown · source age unknown/);
assert.match(line({data: null, age: null, snapshot: false}).text, /· unavailable · source age unknown/);
assert.match(line({url: '/api/ops/v1/alerts', age: 0, bound: 60, state: 'unavailable',
  data: {status: 'unknown', observed_at: skewed, stale_after_s: 60}}).text, /· unavailable · source age 0s/);
assert.match(line({url: '/api/delegate/active', data: {tasks: []}, age: 27, bound: 60, state: 'fresh'}).text,
  /· fresh · source age 27s · bound 60s$/);
// Account rows still show the server's age_s. That number is not the panel age.
const claudeRow = age => ({account: 'Claude a', reading: {status: 'fresh', age_s: age, read_at: '2030-01-01T00:00:00Z'}, plan: {status: 'ok'}});
const subs = {stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(51)]}]};
assert.match(line({url: '/api/runtime/subscriptions', data: subs}).text, /source age unknown/);
assert.match(subscriptionRows(subs, null).groups, /fresh, 51s old/);
assert.match(subscriptionRows({stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(1800)]}]}, null).groups, /STALE, 30m old/);
const partial = {stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(51),
  {account: 'Claude b', reading: {status: 'unknown', age_s: null, reason_code: 'provider_error'}, plan: {status: 'ok'}}]}]};
assert.match(subscriptionRows(partial, null).groups, /NO READING/);
// Without a monotonic stamp, elapsed still never shrinks: a rollback cannot make an expired snapshot fresh.
const wall = {ageHeader: '0', boundHeader: '10', state: 'fresh', receivedAt: now, data: {status: 'ok'}};
assert.equal(panelAgeSeconds(wall, now + 11000), 11);
assert.equal(panelAgeSeconds(wall, now - 20000), 11);
const clock = performance.now;
performance.now = undefined;
assert.equal(Machinery.snapshotClockStamp(), null);
performance.now = clock;
// A wall-clock jump does not change an age stamped with the monotonic clock, and a backward step does not shrink it.
const realPerf = performance.now.bind(performance);
let mono = realPerf();
performance.now = () => mono;
try {
  const stamped = {ageHeader: '200', boundHeader: '210', state: 'fresh', receivedAt: now, receivedMono: mono, data: {status: 'ok'}};
  assert.equal(panelAgeSeconds(stamped, now + 60000), 200);
  mono += 11000;
  assert.equal(panelAgeSeconds(stamped, now), 211);
  mono -= 20000;
  assert.equal(panelAgeSeconds(stamped, now - 60000), 211);
  assert.equal(panelAgeSeconds({ageHeader: '0', receivedAt: Number.NaN, data: {status: 'ok'}}, now), null);
  assert.equal(panelAgeSeconds({ageHeader: '0', receivedAt: now, receivedMono: Number.NaN, data: {status: 'ok'}}, now), null);
} finally {
  performance.now = realPerf;
}
""")



def _swr_app(monkeypatch, tmp_path, handler_factory, *, ttl: float = 10.0, vary: tuple[str, ...] = ()):
    """A one-route app on ``state_helpers.swr_route`` with a fake cache clock."""
    from types import SimpleNamespace

    from fastapi import APIRouter, Depends, FastAPI

    from lu_machinery.api import state_helpers
    from lu_machinery.api.monitor_context import MonitorRuntime

    now = [1000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path), runtime=MonitorRuntime())
    router = APIRouter()
    router.get("/probe")(state_helpers.swr_route("probe", ttl, vary=vary)(handler_factory(Depends(lambda: ctx))))
    probe_app = FastAPI()
    probe_app.include_router(router)
    key_prefix = state_helpers.ctx_scoped_ttl_key(ctx, "swr", "probe", repr(ctx.roots))
    return TestClient(probe_app), now, key_prefix


def test_swr_route_serves_stale_value_with_true_age_and_one_refresh(monkeypatch, tmp_path):
    """Defect class: an expired value served without its age (or as fresh), a reader blocking on
    the recompute, or several stale reads starting more than one refresh."""
    import threading

    from lu_machinery.api import state_helpers

    calls: list[int] = []
    refreshing, release = threading.Event(), threading.Event()

    def factory(ctx_dependency):
        def handler(ctx=ctx_dependency):
            calls.append(len(calls) + 1)
            if len(calls) > 1:
                refreshing.set()
                assert release.wait(5)
            return {"n": len(calls)}

        return handler

    client, now, key = _swr_app(monkeypatch, tmp_path, factory)
    first = client.get("/probe")
    assert first.json() == {"n": 1}
    assert first.headers["X-Snapshot-Age-S"] == "0.000"
    assert first.headers["X-Snapshot-State"] == "fresh"

    now[0] += 25.0
    try:
        for _ in range(3):  # the refresh is held open: none of these may wait for it
            stale = client.get("/probe")
            assert stale.json() == {"n": 1}
            assert stale.headers["X-Snapshot-Age-S"] == "25.000"
            assert stale.headers["X-Snapshot-State"] == "stale"
        assert refreshing.wait(5)
    finally:
        release.set()
    # Join the one refresh flight; a second collection would fail this read.
    refreshed = state_helpers.cache_get_or_compute(key, 10.0, lambda: pytest.fail("second refresh"))
    assert refreshed == {"n": 2}
    assert calls == [1, 2]
    fresh = client.get("/probe")
    assert fresh.json() == {"n": 2}
    assert fresh.headers["X-Snapshot-Age-S"] == "0.000"


def test_swr_route_recomputes_a_dormant_value_instead_of_serving_it(monkeypatch, tmp_path):
    """Defect class: a value nobody read for longer than ttl + DEMAND_WINDOW_S handed to a
    one-shot caller (an hour-old board or task list) when a fresh computation fits the
    bounded dormant wait."""
    from lu_machinery.api import state_helpers

    calls: list[int] = []

    def factory(ctx_dependency):
        def handler(ctx=ctx_dependency):
            calls.append(1)
            return {"n": len(calls)}

        return handler

    client, now, _key = _swr_app(monkeypatch, tmp_path, factory)
    assert client.get("/probe").json() == {"n": 1}
    now[0] += 10.0 + state_helpers.DEMAND_WINDOW_S + 1.0
    dormant = client.get("/probe")
    assert dormant.json() == {"n": 2}
    assert dormant.headers["X-Snapshot-Age-S"] == "0.000"
    assert len(calls) == 2


def test_swr_route_caches_only_default_or_varied_parameters(monkeypatch, tmp_path):
    """Defect class: a filtered query answered from another filter's cache entry."""
    from fastapi import Query

    calls: list[tuple[str, str | None]] = []

    def factory(ctx_dependency):
        def handler(status: str = Query("all"), agent: str | None = Query(None), ctx=ctx_dependency):
            calls.append((status, agent))
            return {"status": status, "agent": agent, "n": len(calls)}

        return handler

    client, _now, _key = _swr_app(monkeypatch, tmp_path, factory, vary=("status",))
    assert client.get("/probe?status=done").json() == {"status": "done", "agent": None, "n": 1}
    assert client.get("/probe?status=failed").json() == {"status": "failed", "agent": None, "n": 2}
    cached = client.get("/probe?status=done")
    assert cached.json() == {"status": "done", "agent": None, "n": 1}
    assert "X-Snapshot-Age-S" in cached.headers
    # A parameter outside ``vary`` is never cached: every call runs the handler.
    for expected in (3, 4):
        direct = client.get("/probe?status=done&agent=codex")
        assert direct.json() == {"status": "done", "agent": "codex", "n": expected}
        assert "X-Snapshot-Age-S" not in direct.headers
    assert len(calls) == 4


def test_swr_route_cached_hit_ignores_saturated_default_executor(tmp_path):
    """Defect class: cached routes wait for a saturated executor or mix cache scopes."""
    import asyncio
    import concurrent.futures
    import threading
    from types import SimpleNamespace

    from fastapi import Response

    from lu_machinery.api.monitor_context import MonitorRuntime
    from lu_machinery.api.state_helpers import swr_route

    calls = []

    @swr_route("executor-saturation-probe", 60, vary=("choice",))
    def route(ctx, choice="a"):
        calls.append((ctx.roots.project_root, choice))
        return str(ctx.roots.project_root), choice

    async def exercise():
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        release = threading.Event()
        started = threading.Event()
        contexts = [
            SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path / name), runtime=MonitorRuntime())
            for name in ("one", "two")
        ]
        combinations = [(contexts[0], "a"), (contexts[0], "b"), (contexts[1], "a")]
        blocked = None
        try:
            for ctx, choice in combinations:
                assert await route(ctx=ctx, choice=choice, _swr_response=Response()) == (
                    str(ctx.roots.project_root), choice
                )

            def block_worker():
                started.set()
                release.wait(timeout=5)

            blocked = loop.run_in_executor(None, block_worker)
            assert started.wait(1)
            for ctx, choice in combinations:
                assert await asyncio.wait_for(
                    route(ctx=ctx, choice=choice, _swr_response=Response()), timeout=0.5
                ) == (str(ctx.roots.project_root), choice)
            assert len(calls) == len(combinations)
        finally:
            release.set()
            if blocked is not None:
                await blocked
            executor.shutdown(wait=True)

    asyncio.run(exercise())
