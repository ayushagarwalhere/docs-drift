/* Try it: edit a tiny CLI and its README, then check the README against the code in a throwaway sandbox. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const code = $('try-code');
  const readme = $('try-readme');
  if (!code || !readme) return;
  const original = $('try-original').value;
  const originalReadme = $('try-readme-original').value;
  const status = $('try-status');
  const list = $('try-findings');
  const summary = $('try-summary');
  const empty = $('try-empty');
  const note = $('try-note');
  const DEBOUNCE_MS = 600;
  let timer = 0;
  let sequence = 0;
  let controller = null;
  let lastChecked = '';

  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const setStatus = (message, tone = '') => { status.textContent = message; status.dataset.tone = tone; };

  // Refactors a real pull request might make. Each one edits the code pane only.
  const SCENARIOS = {
    rename: {
      done: 'Renamed --listen to --bind in app.py.',
      apply: value => value.replace(/(["'])--listen\1/g, '$1--bind$1'),
    },
    default: {
      done: 'Changed the --workers default from 4 to 8 in app.py.',
      apply: value => value.replace(/("--workers",[^\n]*default=)4\b/, (_, head) => `${head}8`),
    },
    env: {
      done: 'Removed the SHIPIT_TOKEN check from app.py.',
      apply: value => value.replace(/\n[ \t]*token = os\.environ\.get\("SHIPIT_TOKEN"\)\n[ \t]*if [^\n]*not token:\n[ \t]*parser\.error\([^\n]*\)\n/, '\n'),
    },
  };

  // Python's str.splitlines() boundaries, so server line numbers map onto textarea offsets.
  const BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g;
  const lineAt = (value, number) => {
    const starts = [0];
    for (const match of value.matchAll(BREAK)) starts.push(match.index + match[0].length);
    if (number < 1 || number > starts.length) return null;
    const start = starts[number - 1];
    const next = number < starts.length ? starts[number] : value.length;
    return { start, body: value.slice(start, next).replace(/(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])$/, '') };
  };
  const unitsBefore = (line, column) => Array.from(line).slice(0, Math.max(0, column - 1)).join('').length;
  const spanFor = (lineNumber, column, needle) => {
    const line = lineAt(readme.value, lineNumber);
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
  const highlight = span => { readme.focus(); readme.setSelectionRange(span.start, span.end); };

  const chip = finding => el('span', `dd-wf-chip is-${finding.verdict}`, finding.label);

  const evidenceRow = ev => {
    const row = el('li');
    const diff = ev.text.includes('\n') && /^[-+]/.test(ev.text);
    if (diff) {
      // History evidence is the changed line pair from the commit, not a single declaration.
      row.className = 'dd-try-diff';
      row.append(el('code', 'dd-wf-file', `${ev.file} · diff`));
      for (const part of ev.text.split('\n').slice(0, 2)) {
        row.append(el('span', part.startsWith('-') ? 'is-del' : 'is-add', part.trim()));
      }
    } else {
      row.append(el('code', 'dd-wf-file', `${ev.file}:${ev.line}`), el('span', '', ev.text.trim()));
    }
    return row;
  };

  const render = (data, checked) => {
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
      top.append(chip(finding), el('span', 'dd-wf-kind', finding.kind.replace(/_/g, ' ')), el('span', 'dd-wf-loc', `README.md:${finding.line}`));
      const claim = el('p', 'dd-wf-claim');
      if (finding.kind === 'default_value') {
        claim.append(el('code', '', finding.claim), el('span', 'dd-wf-arrow', ' default '));
        if (finding.expected_default) claim.append(el('code', finding.is_drift ? 'is-old' : '', finding.expected_default));
        if (finding.suggestion && finding.is_drift) claim.append(el('span', 'dd-wf-arrow', ' → '), el('code', 'is-new', finding.suggestion));
      } else {
        claim.append(el('code', finding.is_drift ? 'is-old' : '', finding.claim));
        if (finding.suggestion && finding.is_drift) claim.append(el('span', 'dd-wf-arrow', ' → '), el('code', 'is-new', finding.suggestion));
      }
      item.append(top, claim);
      if (finding.is_drift) {
        // Say what changed in the code in plain words; the raw rule reason is the fallback.
        const plain = {
          renamed: finding.suggestion && `app.py now calls this ${finding.suggestion}.`,
          default_changed: finding.suggestion && `app.py now defaults to ${finding.suggestion}.`,
        }[finding.verdict];
        const reason = plain || (finding.reasons || [])[0] || finding.message;
        if (reason) item.append(el('p', 'dd-wf-reason', reason));
      }
      if (finding.evidence?.length) {
        const evidence = el('ul', 'dd-wf-evidence');
        evidence.setAttribute('aria-label', 'Evidence in the code');
        for (const ev of finding.evidence.slice(0, 2)) evidence.append(evidenceRow(ev));
        item.append(evidence);
      }
      if (finding.introduced_by) {
        item.append(el('p', 'dd-wf-commit', `Broken by commit ${String(finding.introduced_by.sha).slice(0, 7)} · ${finding.introduced_by.summary}`));
      }
      if (finding.fixed_by) item.append(el('p', 'dd-wf-history', `Fixed together with the ${finding.fixed_by} suggestion.`));
      const actions = el('div', 'dd-wf-actions');
      if (finding.edit) {
        const certain = finding.edit.certain;
        const label = finding.edit.new_text ? `${certain ? 'Apply fix' : 'Review'}: ${finding.edit.old_text.trim()} → ${finding.edit.new_text}` : `${certain ? 'Apply fix' : 'Review'}: remove ${finding.edit.old_text.trim()}`;
        const apply = el('button', `dd-btn ${certain ? 'dd-btn-lime' : 'dd-btn-white'}`, label);
        apply.type = 'button';
        if (!certain) apply.title = 'Not confirmed by the rule fixer; review before keeping it.';
        apply.addEventListener('click', () => {
          if (readme.value !== checked.readme || code.value !== checked.code) { setStatus('The files changed since this check. Re-checking…'); check(true); return; }
          const edits = [finding.edit, ...(finding.other_edits || [])]
            .map(edit => ({ edit, span: spanFor(edit.line, edit.column, edit.old_text) }))
            .filter(entry => entry.span)
            .sort((a, b) => b.span.start - a.span.start);
          if (!edits.length) { setStatus('Could not find that text any more. Re-checking…'); check(true); return; }
          let value = readme.value;
          for (const { edit, span } of edits) value = value.slice(0, span.start) + edit.new_text + value.slice(span.end);
          readme.value = value;
          setStatus(`Fixed README.md line ${finding.edit.line}.`, 'ok');
          check(true);
        });
        actions.append(apply);
      }
      const show = el('button', 'dd-btn dd-btn-ghost', 'Show in README');
      show.type = 'button';
      show.addEventListener('click', () => {
        const span = spanFor(finding.line, finding.column, finding.claim) || (finding.edit && spanFor(finding.edit.line, finding.edit.column, finding.edit.old_text));
        if (span) highlight(span); else setStatus('That text has changed since the last check.');
      });
      actions.append(show);
      item.append(actions);
      list.append(item);
    }
  };

  const check = async (now = false) => {
    clearTimeout(timer);
    if (!now) { timer = setTimeout(() => check(true), DEBOUNCE_MS); return; }
    const checked = { code: code.value, readme: readme.value };
    const key = `${checked.code}\0${checked.readme}`;
    if (key === lastChecked) return;
    controller?.abort();
    controller = new AbortController();
    const mine = ++sequence;
    setStatus('Checking…', 'busy');
    list.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/try/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: checked.code, readme: checked.readme, previous_code: original }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (mine !== sequence) return;
      if (!response.ok) {
        setStatus(typeof data.detail === 'string' ? data.detail : 'The check could not run. Edit a file to retry.', 'error');
        if (response.status === 429) timer = setTimeout(() => check(true), 5000);
        return;
      }
      lastChecked = key;
      render(data, checked);
      setStatus(data.drift ? `${data.drift} README line${data.drift === 1 ? ' is' : 's are'} out of date.` : 'README.md matches app.py.', data.drift ? 'error' : 'ok');
    } catch (failure) {
      if (failure.name !== 'AbortError' && mine === sequence) setStatus('Could not reach the checker. Your edits are kept; edit a file to retry.', 'error');
    } finally {
      if (mine === sequence) list.setAttribute('aria-busy', 'false');
    }
  };

  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    const name = button.dataset.scenario;
    if (name === 'reset') {
      code.value = original; readme.value = originalReadme;
      setStatus('Back to the original files.');
      check(true);
      return;
    }
    const scenario = SCENARIOS[name];
    const next = scenario.apply(code.value);
    if (next === code.value) { setStatus('Already applied. Press Reset to start over.'); return; }
    code.value = next;
    setStatus(scenario.done);
    check(true);
  }));
  for (const pane of [code, readme]) {
    pane.addEventListener('input', () => check());
    pane.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); lastChecked = ''; check(true); }
    });
  }
  $('try-check').addEventListener('click', () => { lastChecked = ''; check(true); });
  check(true);
})();
