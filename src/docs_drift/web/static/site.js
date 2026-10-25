/* docs-drift site interactions. Everything works without JS; this only adds motion and carousels. */
(() => {
  'use strict';
  const root = document.documentElement;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const session = (key, value) => {
    try { if (value === undefined) return sessionStorage.getItem(key); sessionStorage.setItem(key, value); }
    catch { /* storage can be blocked */ }
    return null;
  };

  // Preloader: hold the hero until fonts are ready (longer on the first visit), then play the intro.
  const preloader = document.querySelector('.dd-preloader');
  if (preloader) {
    if (reduced) root.classList.add('dd-ready');
    else {
      root.classList.add('dd-anim');
      const hold = session('dd-seen') ? 150 : 1100;
      const fonts = document.fonts ? document.fonts.ready : Promise.resolve();
      Promise.race([Promise.all([fonts, wait(hold)]), wait(2200)]).then(() => {
        root.classList.add('dd-ready');
        session('dd-seen', '1');
      });
    }
  }

  // Nav: hidden over the home hero, then follows the tone of the section beneath it.
  const nav = document.querySelector('[data-nav]');
  const toned = [...document.querySelectorAll('[data-tone]:not([data-nav])')];
  const hero = document.querySelector('.dd-hero-links');
  let ticking = false;
  const main = document.querySelector('main');
  const footer = document.querySelector('.dd-foot');
  const update = () => {
    ticking = false;
    if (main && footer && document.body.classList.contains('dd-home')) {
      const reveal = Math.max(0, Math.min(1, (innerHeight - main.getBoundingClientRect().bottom) / footer.offsetHeight));
      footer.style.setProperty('--reveal', reveal.toFixed(3));
    }
    if (!nav) return;
    if (nav.classList.contains('is-home') && hero) nav.classList.toggle('is-shown', hero.getBoundingClientRect().bottom < 0);
    const probe = nav.offsetHeight / 2;
    const under = toned.find(section => { const box = section.getBoundingClientRect(); return box.top <= probe && box.bottom > probe; });
    nav.dataset.tone = under ? under.dataset.tone : (document.body.classList.contains('dd-inner') ? 'dark' : 'light');
  };
  const schedule = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule, { passive: true });
  update();

  // Rise-in reveals.
  const rising = [...document.querySelectorAll('[data-rise]')];
  if (!reduced && 'IntersectionObserver' in window && rising.length) {
    root.classList.add('dd-anim');
    const io = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('is-in'); io.unobserve(entry.target); }
    }), { threshold: .15 });
    rising.forEach(element => io.observe(element));
  }

  // Case deck: one card per step, arrows disabled at the ends.
  const track = document.querySelector('[data-deck]');
  if (track) {
    const prev = document.querySelector('[data-deck-prev]');
    const next = document.querySelector('[data-deck-next]');
    const step = () => (track.querySelector('.dd-card')?.offsetWidth || track.clientWidth) + parseFloat(getComputedStyle(track).columnGap || 0);
    const sync = () => {
      const max = track.scrollWidth - track.clientWidth - 4;
      if (prev) prev.disabled = track.scrollLeft <= 4;
      if (next) next.disabled = track.scrollLeft >= max;
    };
    prev?.addEventListener('click', () => track.scrollBy({ left: -step(), behavior: reduced ? 'auto' : 'smooth' }));
    next?.addEventListener('click', () => track.scrollBy({ left: step(), behavior: reduced ? 'auto' : 'smooth' }));
    track.addEventListener('scroll', sync, { passive: true });
    addEventListener('resize', sync, { passive: true });
    sync();
  }

  // Evidence fans: rotate which card sits in front.
  document.querySelectorAll('.dd-card-media').forEach(media => {
    const cards = [...media.querySelectorAll('.dd-ev')];
    const turn = delta => cards.forEach(card => { card.dataset.pos = String((Number(card.dataset.pos) + delta + cards.length) % cards.length); });
    media.querySelector('[data-fan-next]')?.addEventListener('click', () => turn(-1));
    media.querySelector('[data-fan-prev]')?.addEventListener('click', () => turn(1));
    cards.forEach(card => card.addEventListener('click', () => { if (card.dataset.pos !== '0') turn(card.dataset.pos === '1' ? -1 : 1); }));
  });

  // Index: a badge follows the pointer over the list.
  const cursor = document.querySelector('[data-cursor]');
  const listWindow = document.querySelector('.dd-index-window');
  if (cursor && listWindow && matchMedia('(hover: hover)').matches) {
    listWindow.addEventListener('pointermove', event => {
      cursor.style.setProperty('--cx', `${event.clientX}px`);
      cursor.style.setProperty('--cy', `${event.clientY}px`);
      cursor.classList.add('is-on');
    });
    listWindow.addEventListener('pointerleave', () => cursor.classList.remove('is-on'));
  }

  // Quote carousel.
  const quotes = document.querySelector('[data-quotes]');
  if (quotes) {
    const items = [...quotes.querySelectorAll('.dd-quote')];
    const count = quotes.querySelector('[data-quote-count]');
    let index = 0;
    const show = next => {
      index = (next + items.length) % items.length;
      items.forEach((item, i) => { item.classList.toggle('is-active', i === index); item.setAttribute('aria-hidden', String(i !== index)); });
      if (count) count.textContent = String(index + 1);
    };
    quotes.querySelector('[data-quote-prev]')?.addEventListener('click', () => show(index - 1));
    quotes.querySelector('[data-quote-next]')?.addEventListener('click', () => show(index + 1));
    show(0);
  }
})();
