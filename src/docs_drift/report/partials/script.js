const cards = [...document.querySelectorAll('.card')];
const data = JSON.parse(document.getElementById('report-data').textContent);
const root = document.documentElement;
const search = document.getElementById('finding-search');
let selected = -1, cy, activeFilter = () => true;
const token = name => getComputedStyle(root).getPropertyValue(name).trim();
const graphStyles = () => [
  { selector: 'node', style: { label: 'data(label)', 'font-size': 11, 'text-wrap': 'wrap', 'text-max-width': 100, 'background-color': token('--graph-node'), color: token('--muted'), 'text-valign': 'bottom', 'text-margin-y': 7 } },
  { selector: 'node[kind="claim"]', style: { 'background-color': token('--graph-secondary') } },
  { selector: 'node[kind="symbol"]', style: { shape: 'diamond', 'background-color': token('--graph-node') } },
  { selector: 'node[kind="file"],node[kind="commit"]', style: { shape: 'roundrectangle', 'background-color': token('--graph-secondary') } },
  { selector: 'edge', style: { width: 1.5, 'line-color': token('--graph-line'), 'target-arrow-color': token('--graph-line'), 'target-arrow-shape': 'triangle', 'curve-style': 'bezier' } },
  { selector: 'edge[verdict="renamed"]', style: { 'line-style': 'dashed' } },
  { selector: 'edge[verdict="default_changed"]', style: { 'line-style': 'dotted' } },
  { selector: '.active', style: { 'border-width': 3, 'border-color': token('--graph-selected'), 'line-color': token('--graph-selected'), 'target-arrow-color': token('--graph-selected') } },
  { selector: 'edge.active', style: { width: 3 } }
];
const storage = (key, value) => {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { /* Offline and private modes can restrict storage. */ }
  return null;
};
function setTheme(theme) {
  root.dataset.theme = theme;
  const button = document.getElementById('theme');
  button.textContent = theme === 'dark' ? 'Light ◐' : 'Dark ◑';
  button.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
  if (cy) cy.style().fromJson(graphStyles()).update();
}
setTheme(storage('docs-drift-theme') === 'light' ? 'light' : 'dark');
document.getElementById('theme').onclick = () => {
  const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  storage('docs-drift-theme', theme); setTheme(theme);
};
function filter(test) {
  activeFilter = test;
  const query = (search?.value || '').trim().toLowerCase();
  cards.forEach(card => { card.hidden = !test(card) || (query.length > 0 && !card.textContent.toLowerCase().includes(query)); });
  document.getElementById('no-results').hidden = cards.some(card => !card.hidden);
}
document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => {
  document.querySelectorAll('[data-filter]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  filter(card => button.dataset.filter === 'all' || card.dataset.verdict === button.dataset.filter);
});
search?.addEventListener('input', () => filter(activeFilter));
search?.addEventListener('keydown', event => { if (event.key === 'Escape') { search.value = ''; filter(activeFilter); } });
function select(card) {
  cards.forEach(item => item.classList.toggle('selected', item === card)); selected = cards.indexOf(card);
  if (cy) {
    cy.elements().removeClass('active');
    const edges = cy.edges().filter(edge => edge.data('claim') === card.dataset.id);
    edges.addClass('active'); edges.connectedNodes().addClass('active');
  }
}
cards.forEach(card => { card.onclick = () => select(card); card.onfocus = () => select(card); });
document.addEventListener('keydown', event => {
  if (event.ctrlKey || event.metaKey || event.altKey || event.target.isContentEditable || event.target.closest('input, textarea, button, summary, a, select')) return;
  if (event.key === '/' && search) { event.preventDefault(); search.focus(); return; }
  const visible = cards.filter(card => !card.hidden), index = visible.indexOf(cards[selected]);
  if ((event.key === 'j' || event.key === 'k') && visible.length) {
    event.preventDefault();
    const card = visible[(index + (event.key === 'j' ? 1 : -1) + visible.length) % visible.length];
    card.focus(); select(card);
  }
  if (event.key === 'Enter' && cards[selected] && !cards[selected].hidden) {
    event.preventDefault(); const details = cards[selected].querySelector('details'); details.open = !details.open;
  }
});
function tab(insights) {
  document.getElementById('findings-view').hidden = insights; document.getElementById('insights-view').hidden = !insights;
  document.getElementById('findings-tab').setAttribute('aria-pressed', String(!insights));
  document.getElementById('insights-tab').setAttribute('aria-pressed', String(insights));
  if (cy && !insights) cy.resize();
}
document.getElementById('findings-tab').onclick = () => tab(false);
document.getElementById('insights-tab').onclick = () => tab(true);
if (window.cytoscape) {
  document.getElementById('graph-fallback').hidden = true;
  cy = cytoscape({ container: document.getElementById('graph'), elements: [...data.nodes, ...data.edges], style: graphStyles(), layout: { name: 'cose', animate: false, nodeRepulsion: 16000, idealEdgeLength: 90, padding: 30 } });
  cy.on('tap', 'node', event => {
    const id = event.target.id(), ids = new Set(data.edges.filter(edge => edge.data.source === id || edge.data.target === id).map(edge => edge.data.claim));
    filter(card => ids.has(card.dataset.id));
  });
}
document.getElementById('reset').onclick = () => {
  if (search) search.value = '';
  document.querySelectorAll('[data-filter]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')));
  filter(() => true); cards.forEach(card => card.classList.remove('selected')); selected = -1;
  if (cy) { cy.elements().removeClass('active'); cy.fit(undefined, 30); }
};
document.getElementById('copy-share')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  try { await navigator.clipboard.writeText(location.href); button.textContent = 'Copied ✓'; }
  catch { button.textContent = 'Copy unavailable'; }
  setTimeout(() => { button.textContent = 'Copy share URL'; }, 2200);
});
