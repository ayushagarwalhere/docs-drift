/* Write with voice: debounced checks of dictated documentation against a saved static index. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const text = $('write-text');
  if (!text) return;
  const repo = $('write-repo');
  const path = $('write-path');
  const status = $('write-status');
  const list = $('write-findings');
  const summary = $('write-summary');
  const empty = $('write-empty');
  const note = $('write-note');
  const count = $('write-count');
  const sessionGroup = $('write-session');
  const maxBytes = Number(text.dataset.maxBytes) || 20000;
  const DEBOUNCE_MS = 600;
  const encoder = new TextEncoder();
  const samples = new Map([[repo.value, text.value]]);
  let timer = 0;
  let sequence = 0;
  let controller = null;
  let lastChecked = '';
  let pristine = true;

  const store = (key, value) => {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      sessionStorage.setItem(key, value);
    } catch { /* storage can be blocked */ }
    return null;
  };
  const setStatus = (message, tone = '') => {
    status.textContent = message;
    status.dataset.tone = tone;
  };
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };

  // Python's str.splitlines() boundaries, so server line numbers map onto textarea offsets.
  const BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g;
  const lineStarts = value => {
    const starts = [0];
    for (const match of value.matchAll(BREAK)) starts.push(match.index + match[0].length);
    return starts;
  };
  const lineAt = (value, number) => {
    const starts = lineStarts(value);
    if (number < 1 || number > starts.length) return null;
    const start = starts[number - 1];
    const next = number < starts.length ? starts[number] : value.length;
    const body = value.slice(start, next).replace(/(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])$/, '');
    return { start, body };
  };
  // Server columns count code points; textarea offsets count UTF-16 units.
  const unitsBefore = (line, column) => Array.from(line).slice(0, Math.max(0, column - 1)).join('').length;
  const spanFor = (lineNumber, column, needle) => {
    const line = lineAt(text.value, lineNumber);
    if (!line || !needle) return null;
    const near = unitsBefore(line.body, column);
    let index = line.body.startsWith(needle, near) ? near : -1;
    if (index < 0) {
      const hits = [];
      for (let at = line.body.indexOf(needle); at >= 0; at = line.body.indexOf(needle, at + 1)) hits.push(at);
      if (!hits.length) return null;
      index = hits.reduce((best, at) => (Math.abs(at - near) < Math.abs(best - near) ? at : best));
    }
    return { start: line.start + index, end: line.start + index + needle.length };
  };
  const select = span => {
    text.focus();
    text.setSelectionRange(span.start, span.end);
  };
  const replace = (span, value) => {
    select(span);
    // insertText keeps the browser's undo history; setRangeText is the fallback.
    const done = document.queryCommandSupported?.('insertText') && document.execCommand('insertText', false, value);
    if (!done) {
      text.setRangeText(value, span.start, span.end, 'end');
      text.dispatchEvent(new Event('input', { bubbles: true }));
    }
  };

  const updateCount = () => {
    const bytes = encoder.encode(text.value).length;
    count.textContent = `${(bytes / 1000).toFixed(bytes < 10000 ? 1 : 0)} / ${maxBytes / 1000} KB`;
    count.dataset.over = String(bytes > maxBytes);
    return bytes;
  };

  const chip = finding => {
    const node = el('span', `dd-wf-chip is-${finding.verdict}`, finding.label);
    if (finding.is_drift && finding.confidence < 0.6) node.append(el('small', '', ' · low confidence'));
    return node;
  };

  const render = (data, checkedText) => {
    list.replaceChildren();
    const findings = data.findings || [];
    empty.hidden = findings.length > 0;
    const drift = data.drift || 0;
    summary.textContent = findings.length
      ? `${data.claims} claim${data.claims === 1 ? '' : 's'} checked · ${drift ? `${drift} out of date` : 'all match the code'}`
      : 'No checkable claims yet.';
    summary.dataset.tone = drift ? 'drift' : findings.length ? 'ok' : '';
    note.textContent = data.note || '';
    for (const finding of findings) {
      const item = el('li', `dd-wf is-${finding.verdict}`);
      const top = el('div', 'dd-wf-top');
      top.append(chip(finding), el('span', 'dd-wf-kind', finding.kind.replace(/_/g, ' ')), el('span', 'dd-wf-loc', `line ${finding.line}`));
      const claim = el('p', 'dd-wf-claim');
      if (finding.kind === 'default_value') {
        // A default claim names a setting; the drift is in its value, not its name.
        claim.append(el('code', '', finding.claim), el('span', 'dd-wf-arrow', ' default '));
        if (finding.expected_default) claim.append(el('code', finding.is_drift ? 'is-old' : '', finding.expected_default));
        if (finding.suggestion && finding.is_drift) claim.append(el('span', 'dd-wf-arrow', ' → '), el('code', 'is-new', finding.suggestion));
      } else {
        claim.append(el('code', finding.is_drift ? 'is-old' : '', finding.claim));
        if (finding.suggestion && finding.is_drift) {
          claim.append(el('span', 'dd-wf-arrow', ' → '), el('code', 'is-new', finding.suggestion));
        }
      }
      item.append(top, claim);
      const reason = (finding.reasons || [])[0];
      if (reason) item.append(el('p', 'dd-wf-reason', reason));
      if (!finding.introduced_by) {
        for (const extra of (finding.reasons || []).slice(1).filter(r => /saved scan/i.test(r))) item.append(el('p', 'dd-wf-history', extra));
      }
      if (finding.candidate) {
        const hint = el('p', 'dd-wf-history', 'Did you mean ');
        hint.append(el('code', 'is-new', finding.candidate.name), document.createTextNode(` (${finding.candidate.file}:${finding.candidate.line})? Closest name in the code, not a confirmed rename.`));
        item.append(hint);
      }
      if (finding.evidence?.length) {
        const evidence = el('ul', 'dd-wf-evidence');
        evidence.setAttribute('aria-label', 'Evidence in the code');
        for (const ev of finding.evidence.slice(0, 2)) {
          const row = el('li');
          row.append(el('code', 'dd-wf-file', `${ev.file}:${ev.line}`), el('span', '', ev.text));
          evidence.append(row);
        }
        item.append(evidence);
      }
      if (finding.introduced_by) {
        // Write checks never run Git; a commit here comes from the saved scan of the same claim.
        const commit = el('p', 'dd-wf-commit', `Changed in ${finding.introduced_by.sha} · ${finding.introduced_by.summary}`);
        commit.append(el('span', '', ' — from the saved scan’s Git history'));
        item.append(commit);
      }
      const actions = el('div', 'dd-wf-actions');
      if (finding.edit) {
        const apply = el('button', `dd-btn ${finding.edit.certain ? 'dd-btn-lime' : 'dd-btn-white'}`, finding.edit.certain ? 'Apply suggestion' : `Use ${finding.edit.new_text}`);
        apply.type = 'button';
        apply.setAttribute('aria-label', `Replace ${finding.edit.old_text} with ${finding.edit.new_text} on line ${finding.edit.line}`);
        if (!finding.edit.certain) apply.title = 'Not confirmed by the rule fixer; review before keeping it.';
        apply.addEventListener('click', () => {
          if (text.value !== checkedText) { setStatus('The draft changed since this check. Re-checking…'); check(true); return; }
          const edits = [finding.edit, ...(finding.other_edits || [])]
            .map(edit => ({ edit, span: spanFor(edit.line, edit.column, edit.old_text) }))
            .filter(item => item.span)
            .sort((a, b) => b.span.start - a.span.start);
          if (!edits.length) { setStatus('Could not find that text any more. Re-checking…'); check(true); return; }
          for (const { edit, span } of edits) replace(span, edit.new_text);
          setStatus(`Replaced ${finding.edit.old_text} with ${finding.edit.new_text}.`, 'ok');
          check(true);
        });
        actions.append(apply);
      }
      const show = el('button', 'dd-btn dd-btn-ghost', 'Show in text');
      show.type = 'button';
      show.addEventListener('click', () => {
        const span = spanFor(finding.line, finding.column, finding.claim) || (finding.edit && spanFor(finding.edit.line, finding.edit.column, finding.edit.old_text));
        if (span) select(span); else setStatus('That text has changed since the last check.');
      });
      actions.append(show);
      item.append(actions);
      list.append(item);
    }
    if (data.truncated) list.append(el('li', 'dd-wf-more', 'Showing the first 100 findings.'));
  };

  const check = async (now = false) => {
    clearTimeout(timer);
    if (!now) { timer = setTimeout(() => check(true), DEBOUNCE_MS); return; }
    const value = text.value;
    if (updateCount() > maxBytes) { setStatus(`The draft is over ${maxBytes / 1000} KB. Split it to keep checking.`, 'error'); return; }
    if (!value.trim()) {
      controller?.abort();
      sequence += 1;
      lastChecked = ''; // The panel is cleared, so the same text dictated again must re-check.
      list.replaceChildren(); empty.hidden = false; summary.textContent = 'Claims appear here as you speak.'; summary.dataset.tone = '';
      setStatus('Ready.'); return;
    }
    const key = `${repo.value}\0${path.value}\0${value}`;
    if (key === lastChecked) return;
    controller?.abort();
    controller = new AbortController();
    const mine = ++sequence;
    setStatus('Checking…', 'busy');
    list.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/write/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scan_id: repo.value, path: path.value.trim() || 'docs/new.md', text: value }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (mine !== sequence) return;
      if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : 'The check could not run. Edit the text to retry.';
        setStatus(detail, 'error');
        if (response.status === 429) timer = setTimeout(() => check(true), 5000);
        return;
      }
      lastChecked = key;
      render(data, value);
      setStatus(`Checked against ${data.repo} at ${String(data.head_sha || '').slice(0, 7)}.`, 'ok');
    } catch (failure) {
      if (failure.name !== 'AbortError' && mine === sequence) setStatus('Could not reach the checker. Your text is kept; edit it to retry.', 'error');
    } finally {
      if (mine === sequence) list.setAttribute('aria-busy', 'false');
    }
  };

  const saveDraft = () => store('dd-write-draft', JSON.stringify({ scan_id: repo.value, text: text.value }));
  text.addEventListener('input', () => { pristine = false; updateCount(); saveDraft(); check(); });
  text.addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); lastChecked = ''; check(true); }
  });
  path.addEventListener('change', () => check(true));
  $('write-form').addEventListener('submit', event => { event.preventDefault(); check(true); });
  $('write-check').addEventListener('click', () => { lastChecked = ''; check(true); });
  $('write-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text.value);
    } catch {
      text.focus(); text.select(); document.execCommand('copy');
    }
    setStatus('Copied the draft as Markdown.', 'ok');
  });

  const meta = async ident => {
    const response = await fetch(`/api/write/repos/${encodeURIComponent(ident)}`);
    if (!response.ok) return null;
    return response.json();
  };
  const loadSample = async (force = false) => {
    let sample = samples.get(repo.value);
    if (sample === undefined) {
      const info = await meta(repo.value).catch(() => null);
      sample = info?.sample || '';
      samples.set(repo.value, sample);
    }
    if (sample && (force || pristine || !text.value.trim())) {
      text.value = sample; pristine = true; updateCount();
    }
  };
  $('write-sample').addEventListener('click', async () => { await loadSample(true); lastChecked = ''; check(true); });
  repo.addEventListener('change', async () => {
    const option = repo.selectedOptions[0];
    if (option?.dataset.reason) setStatus(option.dataset.reason, 'error');
    await loadSample(false);
    saveDraft();
    lastChecked = '';
    check(true);
  });

  // Repositories scanned live in this browser session (recorded by the scanner on the home page).
  const addSessionScans = async () => {
    const ids = [];
    const wanted = new URLSearchParams(location.search).get('scan');
    if (wanted && /^[A-Za-z0-9-]{1,64}$/.test(wanted)) ids.push(wanted);
    try {
      for (const item of JSON.parse(store('dd-scans') || '[]')) if (item && /^[A-Za-z0-9-]{1,64}$/.test(item.scan_id)) ids.push(item.scan_id);
    } catch { /* ignore malformed storage */ }
    const known = new Set([...repo.options].map(option => option.value));
    for (const ident of [...new Set(ids)].slice(0, 8)) {
      if (known.has(ident)) { if (ident === wanted) repo.value = ident; continue; }
      const info = await meta(ident).catch(() => null);
      if (!info) continue;
      const option = el('option', '', info.available ? info.name : `${info.name} (unavailable)`);
      option.value = ident;
      if (!info.available) { option.disabled = true; option.dataset.reason = info.reason || ''; }
      sessionGroup.append(option);
      sessionGroup.hidden = false;
      if (ident === wanted && info.available) { repo.value = ident; pristine = false; }
      else if (ident === wanted) setStatus(info.reason || 'That scan cannot be used for write checks.', 'error');
    }
  };

  const restore = () => {
    try {
      const draft = JSON.parse(store('dd-write-draft') || 'null');
      if (draft && typeof draft.text === 'string' && draft.text.trim() && [...repo.options].some(o => o.value === draft.scan_id && !o.disabled)) {
        repo.value = draft.scan_id; text.value = draft.text; pristine = false;
      }
    } catch { /* ignore malformed storage */ }
  };

  updateCount();
  addSessionScans().finally(() => {
    if (!new URLSearchParams(location.search).get('scan')) restore();
    updateCount();
    check(true);
  });
})();
