/* Dirigent UI: детали заказа. DAG-представление = таблица шагов + инспектор. */
const params = new URLSearchParams(location.search);
const oid = params.get('id') || '';
document.getElementById('oid').textContent = oid;
const conn = document.getElementById('conn');
const tbody = document.querySelector('#steps tbody');
const insp = document.getElementById('inspector');
let current = null;

function esc(s) { return String(s ?? '').replace(/[&<>"]/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }

async function load() {
  const r = await fetch('/orders/' + encodeURIComponent(oid));
  if (!r.ok) throw new Error('rest ' + r.status);
  return r.json();
}

function render(o) {
  document.getElementById('goal').textContent = o.goal + '  [' + o.state + ']';
  tbody.innerHTML = (o.steps || []).map(s =>
    `<tr data-sid="${esc(s.id)}">
       <td><code>${esc(s.id)}</code></td>
       <td><span class="badge ${esc(s.state)}">${esc(s.state)}</span></td>
       <td>${esc(s.type)}</td><td>${s.attempts}</td>
       <td>${esc((s.error_text || '').slice(0, 80))}</td></tr>`).join('');
  tbody.querySelectorAll('tr').forEach(tr => tr.addEventListener('click', () => {
    const s = (o.steps || []).find(x => x.id === tr.dataset.sid);
    if (!s) return;
    insp.innerHTML = `<b>${esc(s.id)}</b> [${esc(s.state)}] попыток: ${s.attempts}\n\n` +
      `тип: ${esc(s.type)}\n` +
      (s.error_text ? `ошибка: ${esc(s.error_text)}\n` : '') +
      (s.log_tail ? `\n--- log_tail ---\n${esc(s.log_tail)}` : '');
  }));
}

async function refresh() {
  try { current = await load(); render(current);
        conn.textContent = 'обновлено ' + new Date().toLocaleTimeString();
        conn.className = 'conn ok'; }
  catch (e) { conn.textContent = 'REST недоступен: ' + e.message; conn.className = 'conn lost'; }
}

let es;
function connectSSE() {
  if (es) es.close();
  es = new EventSource('/api/v1/events?order_id=' + encodeURIComponent(oid));
  es.onerror = () => { conn.className = 'conn lost'; };
  es.onopen = () => { conn.className = 'conn ok'; };
  es.onmessage = () => refresh();
}

refresh().then(connectSSE);
setInterval(refresh, 15000);
