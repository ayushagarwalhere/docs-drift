/* Progressive enhancement: the samples and evidence remain usable without motion or JavaScript. */
(() => {
  'use strict';
  const root = document.documentElement;
  const stored = (key, value) => {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
    catch { /* Storage can be unavailable in private browsing. */ }
    return null;
  };
  const themeButton = document.getElementById('theme');
  const setTheme = theme => {
    root.dataset.theme = theme;
    themeButton?.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
    if (themeButton) themeButton.textContent = theme === 'dark' ? 'Light ◐' : 'Dark ◑';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#080808' : '#fafafa');
  };
  setTheme(stored('docs-drift-theme') === 'light' ? 'light' : 'dark');
  themeButton?.addEventListener('click', () => {
    const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    stored('docs-drift-theme', theme); setTheme(theme);
  });

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const motionButton = document.getElementById('motion');
  const reveals = [...document.querySelectorAll('[data-reveal]')];
  const scene = document.querySelector('[data-depth-scene]');
  const panel = document.querySelector('[data-depth-panel]');
  let motion = false, frame = 0;
  const clamp = (number, min, max) => Math.max(min, Math.min(max, number));
  const updateScroll = () => {
    frame = 0;
    const maxScroll = document.documentElement.scrollHeight - innerHeight;
    root.style.setProperty('--page-scroll', maxScroll > 0 ? clamp(scrollY / maxScroll, 0, 1) : 0);
    if (!motion) return;
    if (scene && innerWidth > 580) {
      const box = scene.getBoundingClientRect();
      if (box.bottom > -100 && box.top < innerHeight + 100) {
        const amount = clamp((innerHeight * .45 - box.top) / innerHeight, -.4, 1);
        scene.style.setProperty('--scene-shift', (amount * 12).toFixed(2));
        scene.style.setProperty('--scene-y', (amount * 120).toFixed(2));
      }
    }
    if (panel && innerWidth > 580) {
      const box = panel.getBoundingClientRect();
      panel.style.setProperty('--panel-turn', (clamp((box.top - innerHeight * .3) / innerHeight, -1, 1) * -5).toFixed(2));
    }
  };
  const scheduleScroll = () => { if (!frame) frame = requestAnimationFrame(updateScroll); };
  const setMotion = () => {
    motion = !reducedMotion.matches && stored('docs-drift-motion') !== 'off';
    root.dataset.motion = motion ? 'on' : 'off';
    if (motionButton) {
      motionButton.textContent = reducedMotion.matches ? 'Motion reduced' : `Motion ${motion ? 'on' : 'off'}`;
      motionButton.setAttribute('aria-pressed', String(motion));
      motionButton.disabled = reducedMotion.matches;
    }
    if (!motion) {
      scene?.style.setProperty('--scene-shift', 0); scene?.style.setProperty('--scene-y', 0);
      scene?.style.setProperty('--pointer-x', 0); scene?.style.setProperty('--pointer-y', 0);
      panel?.style.setProperty('--panel-turn', 0);
      reveals.forEach(element => element.classList.add('is-visible'));
    }
    scheduleScroll();
  };
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target); }
    }), { threshold: .06, rootMargin: '0px 0px -30px 0px' });
    reveals.forEach(element => observer.observe(element));
  } else reveals.forEach(element => element.classList.add('is-visible'));
  motionButton?.addEventListener('click', () => { stored('docs-drift-motion', motion ? 'off' : 'on'); setMotion(); });
  reducedMotion.addEventListener('change', setMotion);
  addEventListener('scroll', scheduleScroll, { passive: true });
  addEventListener('resize', scheduleScroll, { passive: true });
  if (scene && matchMedia('(pointer: fine)').matches) {
    scene.addEventListener('pointermove', event => {
      if (!motion || innerWidth < 821) return;
      const box = scene.getBoundingClientRect();
      scene.style.setProperty('--pointer-x', (((event.clientX - box.left) / box.width - .5) * 7).toFixed(2));
      scene.style.setProperty('--pointer-y', (((event.clientY - box.top) / box.height - .5) * -5).toFixed(2));
    });
    scene.addEventListener('pointerleave', () => { scene.style.setProperty('--pointer-x', 0); scene.style.setProperty('--pointer-y', 0); });
  }
  setMotion();

  const sections = [...document.querySelectorAll('.chapter[id]')];
  if (sections.length && 'IntersectionObserver' in window) {
    const navObserver = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      document.querySelectorAll('.site-nav a').forEach(link => {
        if (link.getAttribute('href') === `/#${entry.target.id}`) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }), { rootMargin: '-15% 0px -65% 0px' });
    sections.forEach(section => navObserver.observe(section));
  }

  document.getElementById('copy-install')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    try {
      await navigator.clipboard.writeText(document.getElementById('install-command').textContent.trim());
      button.textContent = 'Copied ✓'; setTimeout(() => { button.textContent = 'Copy ↗'; }, 2200);
    } catch { button.textContent = 'Select to copy'; }
  });

  const form = document.getElementById('scan-form');
  const input = document.getElementById('repo-url');
  const button = document.getElementById('scan-button');
  const error = document.getElementById('scan-error');
  let submitting = false;
  const normalizeRepo = value => {
    const trimmed = value.trim();
    if (/^github\.com\//i.test(trimmed)) return `https://${trimmed}`;
    if (/^[a-z\d_.-]+\/[a-z\d_.-]+$/i.test(trimmed)) return `https://github.com/${trimmed}`;
    return trimmed;
  };
  const errorMessage = detail => typeof detail === 'string' ? detail : 'Enter a public GitHub repository, such as github.com/owner/repository.';
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    submitting = true; button.disabled = true; button.textContent = 'Starting scan…';
    form.setAttribute('aria-busy', 'true'); error.hidden = true; input.removeAttribute('aria-invalid');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/scans', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repo_url: normalizeRepo(input.value) }), signal: controller.signal });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 422) input.setAttribute('aria-invalid', 'true');
        throw Error(errorMessage(data.detail));
      }
      if (!data.scan_id) throw Error('The scan could not be started. Try an instant sample or retry.');
      location.assign(`/scan/${encodeURIComponent(data.scan_id)}`);
    } catch (failure) {
      error.textContent = failure.name === 'AbortError' ? 'The service is taking longer to respond. Try an instant sample, or retry shortly.' : failure.message || 'Could not reach the scanner. Try an instant sample.';
      error.hidden = false;
    } finally {
      clearTimeout(timeout); submitting = false; button.disabled = false; button.textContent = 'Scan repository ↗'; form.setAttribute('aria-busy', 'false');
    }
  });
  document.querySelectorAll('[data-repo]').forEach(shortcut => shortcut.addEventListener('click', () => {
    if (!form || submitting) return;
    input.value = shortcut.dataset.repo; form.requestSubmit();
  }));
  if (form) fetch('/api/stats').then(response => response.json()).then(data => {
    document.getElementById('live-notice').hidden = data.live_available;
    document.getElementById('service-status').textContent = data.live_available ? 'Ready / rule-based' : 'Samples available / live paused';
  }).catch(() => {
    document.getElementById('service-status').textContent = 'Connection not verified';
  });

  const statusPage = document.querySelector('[data-scan-id]');
  if (statusPage) {
    const stages = ['queued', 'cloning', 'indexing', 'matching', 'history', 'done'];
    let timer;
    const poll = async () => {
      try {
        const response = await fetch(`/api/scans/${encodeURIComponent(statusPage.dataset.scanId)}`);
        const data = await response.json();
        if (!response.ok) throw Error(errorMessage(data.detail));
        if (data.status === 'done') { location.reload(); return; }
        document.getElementById('stage-name').textContent = data.status === 'failed' ? 'Could not finish' : data.status.charAt(0).toUpperCase() + data.status.slice(1);
        document.getElementById('progress').value = data.progress;
        document.getElementById('progress-percent').textContent = `${data.progress}%`;
        document.querySelectorAll('[data-stage]').forEach(element => {
          element.classList.toggle('done', stages.indexOf(element.dataset.stage) < stages.indexOf(data.status));
          if (element.dataset.stage === data.status) element.setAttribute('aria-current', 'step');
          else element.removeAttribute('aria-current');
        });
        if (data.counts?.claims !== undefined) document.getElementById('counts').textContent = `Found ${data.counts.claims} claims across ${data.counts.docs} docs${data.counts.symbols !== undefined ? `; indexed ${data.counts.symbols} symbols` : ''}.`;
        if (data.status === 'failed') {
          document.getElementById('status-title').textContent = 'This scan needs another try.';
          error.textContent = `${data.error.message} ${data.error.hint}`; error.hidden = false; return;
        }
        error.hidden = true; timer = setTimeout(poll, 1000);
      } catch (failure) {
        error.textContent = failure.message || 'Connection interrupted. Refresh to reconnect; your scan can still finish.';
        error.hidden = false;
      }
    };
    addEventListener('pagehide', () => clearTimeout(timer), { once: true });
    poll();
  }
})();
