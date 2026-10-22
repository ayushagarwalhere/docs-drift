// DOM behavior checks. No browser, screenshots, layout or accessibility-score claims.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
if (!process.argv[2]) throw Error('Usage: node scripts/check_ui.cjs <linkedom-directory> [base-url]');
const {parseHTML} = require(path.resolve(process.argv[2]));
const base = process.argv[3] || 'http://127.0.0.1:8080';
const demoScript = fs.readFileSync('src/docs_drift/web/static/demo.js', 'utf8');
const reportScript = fs.readFileSync('src/docs_drift/report/partials/script.js', 'utf8');
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
function runtime(html, reduced = false) {
  const {document, Event} = parseHTML(html);
  const saved = new Map(); const timers = new Map(); let tid = 0;
  const calls = [], navigation = [];
  const context = { document, console, Event, AbortController, innerHeight:900, innerWidth:1440, scrollY:0,
    localStorage:{ getItem:key => saved.get(key) ?? null, setItem:(key,value) => saved.set(key,value) },
    matchMedia:query => ({matches:query.includes('reduced-motion') && reduced, addEventListener() {}}),
    requestAnimationFrame:() => 1, addEventListener() {},
    setTimeout:fn => { timers.set(++tid, fn); return tid; }, clearTimeout:id => timers.delete(id),
    getComputedStyle:() => ({getPropertyValue:() => '#aaa'}),
    location:{href:'http://localhost/report', assign:url => navigation.push(url), reload() {}},
    navigator:{clipboard:{writeText:async () => {}}},
    fetch:async (url,options) => { calls.push({url, options}); return {ok:true,json:async () => url === '/api/stats' ? {live_available:true} : {scan_id:'test-id'}}; }
  };
  context.window = context;
  vm.createContext(context);
  return {document, Event, saved, calls, navigation, context, run:source => vm.runInContext(source,context), timers};
}
(async () => {
  const pages = await Promise.all(['/', '/scan/example-rich-cli-reviewed'].map(async route => {
    const response = await fetch(new URL(route, base));
    assert.equal(response.status, 200, route);
    return response.text();
  }));
  const [landing, report] = pages;
  const app = runtime(landing); app.run(demoScript); await flush();
  assert.equal(app.document.documentElement.dataset.theme,'dark');
  app.document.getElementById('theme').click();
  assert.equal(app.document.documentElement.dataset.theme,'light');
  assert.equal(app.saved.get('docs-drift-theme'),'light');
  assert.equal(app.document.documentElement.dataset.motion,'on');
  app.document.getElementById('motion').click();
  assert.equal(app.document.documentElement.dataset.motion,'off');
  assert.equal(app.saved.get('docs-drift-motion'),'off');
  const form = app.document.getElementById('scan-form');
  app.document.getElementById('repo-url').value = 'textualize/rich-cli';
  form.dispatchEvent(new app.Event('submit',{cancelable:true}));
  form.dispatchEvent(new app.Event('submit',{cancelable:true}));
  await flush();
  const posts=app.calls.filter(call => call.options?.method === 'POST');
  assert.equal(posts.length,1);
  assert.equal(JSON.parse(posts[0].options.body).repo_url,'https://github.com/textualize/rich-cli');
  assert.equal(app.navigation[0],'/scan/test-id');
  assert.equal(form.getAttribute('aria-busy'),'false');
  app.context.fetch = async () => ({ok:false,status:422,json:async () => ({detail:'Public GitHub repositories only.'})});
  form.dispatchEvent(new app.Event('submit',{cancelable:true})); await flush();
  assert.equal(app.document.getElementById('scan-error').hidden,false);
  assert.equal(app.document.getElementById('scan-error').textContent,'Public GitHub repositories only.');
  assert.equal(app.document.getElementById('repo-url').getAttribute('aria-invalid'),'true');
  assert.equal(app.document.getElementById('scan-button').disabled,false);
  const reduced = runtime(landing,true); reduced.run(demoScript); await flush();
  assert.equal(reduced.document.documentElement.dataset.motion,'off');
  assert.equal(reduced.document.getElementById('motion').disabled,true);
  const offline = runtime(landing); offline.context.localStorage={getItem(){throw Error('blocked');},setItem(){throw Error('blocked');}};
  offline.run(demoScript); await flush(); assert.equal(offline.document.documentElement.dataset.theme,'dark');
  const view = runtime(report); view.run(reportScript);
  const cards=[...view.document.querySelectorAll('.card')]; assert(cards.length > 1);
  const search=view.document.getElementById('finding-search');
  search.value='no-such-doc-token-xyz'; search.dispatchEvent(new view.Event('input'));
  assert(cards.every(card => card.hidden)); assert.equal(view.document.getElementById('no-results').hidden,false);
  search.value='line-number'; search.dispatchEvent(new view.Event('input'));
  assert(cards.some(card => !card.hidden));
  const verdict=cards.find(card => !card.hidden).dataset.verdict;
  const filter=view.document.querySelector(`[data-filter="${verdict}"]`); filter.click();
  assert(cards.filter(card => !card.hidden).every(card => card.dataset.verdict === verdict && card.textContent.includes('line-number')));
  view.document.getElementById('reset').click();
  assert.equal(search.value,''); assert(cards.every(card => !card.hidden));
  view.document.getElementById('insights-tab').click(); assert.equal(view.document.getElementById('findings-view').hidden,true);
  view.document.getElementById('findings-tab').click(); assert.equal(view.document.getElementById('findings-view').hidden,false);
  view.document.getElementById('theme').click(); assert.equal(view.document.documentElement.dataset.theme,'light');
  cards[0].click();
  const link=view.document.querySelector('a');
  const enter=new view.Event('keydown',{bubbles:true,cancelable:true}); enter.key='Enter'; link.dispatchEvent(enter);
  assert.equal(enter.defaultPrevented,false);
  console.log('PASS: theme/storage; motion/reduced motion; URL normalization; duplicate-submit guard; scan navigation; inline validation/recovery; combined report search/filter; reset; tabs; link keyboard navigation. DOM checks only, no layout claim.');
})().catch(error => {console.error(error);process.exitCode=1;});
