/* Dirigent UI: список заказов. Snapshot из REST, живые обновления по SSE. */
const conn = document.getElementById('conn');
const tbody = document.querySelector('#orders tbody');

async function snapshot() {
  const r = await fetch('/orders');
  if (!r.ok) throw new Error('rest ' + r.status);
  return r.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }

function render(orders) {
  tbody.innerHTML = orders.map(o => {
    const p = o.progress || {};
    return `<tr data-id="${esc(o.id)}" title="открыть">
      <td><code>${esc(o.id.slice(0, 16))}</code></td>
      <td><span class="badge ${esc(o.state)}">${esc(o.state)}</span></td>
      <td>${esc(o.goal)}</td>
      <td>${p.done ?? '?'}/${p.total ?? '?'}</td></tr>`;
  }).join('');
  tbody.querySelectorAll('tr').forEach(tr =>
    tr.addEventListener('click', () =>
      location.href = '/ui/order.html?id=' + tr.dataset.id));
}

async function refresh() {
  try { render(await snapshot()); conn.textContent = 'обновлено ' + new Date().toLocaleTimeString();
        conn.className = 'conn ok'; }
  catch (e) { conn.textContent = 'REST недоступен: ' + e.message; conn.className = 'conn lost'; }
}

let es;
function connectSSE() {
  if (es) es.close();
  es = new EventSource('/api/v1/events');
  es.onopen = () => { conn.className = 'conn ok'; };
  es.onerror = () => { conn.className = 'conn lost'; };
  es.onmessage = () => refresh();          // любое событие -> перечитать snapshot
}

refresh().then(connectSSE);
setInterval(refresh, 15000);               // подстраховка при молчащем SSE
