/* Дирижёр — статический read-only веб-визуализатор заказов (P3a-2).
 *
 * Правила:
 *  - Только GET-запросы к существующему REST API. Никаких mutations, никакого SSE.
 *  - Вся разметка строится через createElement / textContent. innerHTML для
 *    пользовательских строк не используется (защита от XSS).
 *  - Состояния загрузки/пустоты/ошибки отображаются явно.
 */
(() => {
  "use strict";

  const STATE_LABELS = {
    new: "новый",
    active: "активен",
    paused: "пауза",
    waiting_human: "ждёт человека",
    done: "готов",
    error: "ошибка",
    cancelled: "отменён",
    pending: "ожидает",
    ready: "готов к запуску",
    running: "выполняется",
    waiting_resource: "ждёт ресурс",
    waiting_approval: "ждёт апрува",
    waiting_quota: "ждёт квоту",
    retry_scheduled: "повтор запланирован",
    completed: "выполнен",
    failed: "провален",
  };

  const STATE_ORDER = [
    "pending", "ready", "running", "waiting_resource", "waiting_approval",
    "waiting_quota", "retry_scheduled", "completed", "failed", "cancelled",
  ];

  const $ = (id) => document.getElementById(id);

  const els = {
    listStatus: $("list-status"),
    orders: $("orders"),
    refresh: $("refresh"),
    detail: $("detail"),
    detailHint: $("detail-hint"),
    dId: $("d-id"), dGoal: $("d-goal"), dState: $("d-state"),
    dProgress: $("d-progress"), dRepo: $("d-repo"),
    stepList: $("step-list"),
    dag: $("dag"),
    dagNote: $("dag-note"),
    inspEmpty: $("insp-empty"),
    inspBody: $("insp-body"),
    iId: $("i-id"), iType: $("i-type"), iState: $("i-state"),
    iAttempts: $("i-attempts"), iDeps: $("i-deps"), iPrompt: $("i-prompt"),
    iError: $("i-error"), iLog: $("i-log"),
  };

  const NS = "http://www.w3.org/2000/svg";

  // REST API смонтирован в корне origin (/orders, ...). Страница живёт в /ui/,
  // поэтому обращаемся по абсолютному пути от корня, а не от текущего location.
  const API = "/orders";

  /** Текущее состояние: загруженный заказ и выбранный шаг. */
  const view = { order: null, selectedStep: null };

  function setStatus(text, kind) {
    els.listStatus.textContent = text || "";
    if (kind) els.listStatus.dataset.kind = kind;
    else delete els.listStatus.dataset.kind;
  }

  function label(state) {
    return STATE_LABELS[state] || String(state || "—");
  }

  /** Текстовая метка статуса с цветом по data-state. */
  function stateNode(state, extraClass) {
    const span = document.createElement("span");
    span.className = "st" + (extraClass ? " " + extraClass : "");
    span.dataset.state = state || "pending";
    span.textContent = label(state);
    return span;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  async function getJson(url) {
    const resp = await fetch(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });    if (!resp.ok) {
      let detail = "";
      try {
        const body = await resp.json();
        if (body && typeof body.error === "string") detail = body.error;
      } catch (_) { /* тело не JSON — оставляем пустым */ }
      const err = new Error(detail || ("HTTP " + resp.status));
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  function progressText(p) {
    if (!p || typeof p.total !== "number") return "—";
    const done = Number(p.done || 0);
    return `${done} из ${p.total} завершено`;
  }

  /** Деталка не отдаёт progress (только список) — считаем локально из шагов. */
  function progressForOrder(order) {
    if (order.progress && typeof order.progress.total === "number") {
      return order.progress;
    }
    const steps = Array.isArray(order.steps) ? order.steps : [];
    const done = steps.filter((s) => s.state === "completed").length;
    return { total: steps.length, done };
  }

  /* ── Список заказов ─────────────────────────────────────── */

  function renderEmptyList(message) {
    clear(els.orders);
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = message;
    els.orders.appendChild(li);
  }

  function renderOrders(list) {
    clear(els.orders);
    if (!Array.isArray(list) || list.length === 0) {
      renderEmptyList("Заказов нет.");
      return;
    }
    for (const o of list) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "order-btn";
      btn.dataset.oid = o.id;

      const goal = document.createElement("span");
      goal.className = "goal";
      goal.textContent = o.goal || "(без цели)";

      const meta = document.createElement("span");
      meta.className = "meta";

      const oid = document.createElement("span");
      oid.className = "oid";
      oid.textContent = o.id;
      meta.appendChild(oid);

      meta.appendChild(stateNode(o.state));

      const prog = document.createElement("span");
      prog.textContent = progressText(o.progress);
      meta.appendChild(prog);

      btn.appendChild(goal);
      btn.appendChild(meta);
      btn.addEventListener("click", () => loadOrder(o.id));
      li.appendChild(btn);
      els.orders.appendChild(li);
    }
  }

  async function loadOrders() {
    setStatus("Загрузка заказов…");
    els.refresh.disabled = true;
    try {
      const list = await getJson(API);
      renderOrders(list);
      setStatus(list.length ? `${list.length} заказ(ов).` : "");
      // Автовыбор из хэша, если задан #order=<id>.
      const fromHash = hashOrderId();
      if (fromHash) await loadOrder(fromHash, { fromRoute: true });
    } catch (e) {
      renderEmptyList("Не удалось загрузить список.");
      setStatus("Ошибка загрузки заказов: " + e.message, "error");
    } finally {
      els.refresh.disabled = false;
    }
  }

  /* ── Деталка заказа ─────────────────────────────────────── */

  function hashOrderId() {
    const m = /(?:^#|\/)order[=\/]([^&/]+)/.exec(location.hash || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  async function loadOrder(oid, opts) {
    opts = opts || {};
    view.order = null;
    view.selectedStep = null;
    els.detail.hidden = false;
    els.detailHint.textContent = "Загрузка заказа…";
    highlightListSelection(oid);
    try {
      const order = await getJson(API + "/" + encodeURIComponent(oid));
      view.order = order;
      els.detailHint.textContent = "";
      renderOrder(order);
      if (!opts.fromRoute) {
        location.hash = "order=" + encodeURIComponent(oid);
      }
    } catch (e) {
      els.detailHint.textContent = e.status === 404
        ? "Заказ не найден."
        : "Ошибка загрузки заказа: " + e.message;
      els.detailHint.dataset.kind = "error";
      clear(els.stepList);
      clear(els.dag);
      showInspector(null);
    }
  }

  function highlightListSelection(oid) {
    for (const btn of els.orders.querySelectorAll(".order-btn")) {
      btn.setAttribute("aria-current", btn.dataset.oid === oid ? "true" : "false");
    }
  }

  function renderOrder(order) {
    els.dId.textContent = order.id || "—";
    els.dGoal.textContent = order.goal || "—";
    clear(els.dState);
    els.dState.appendChild(stateNode(order.state));
    els.dProgress.textContent = progressText(progressForOrder(order));
    els.dRepo.textContent = order.repo || "—";

    const steps = Array.isArray(order.steps) ? order.steps.slice() : [];
    renderChips(steps);
    renderDag(steps);
    showInspector(null);
  }

  function stepDepends(step) {
    // API P3a-1 не отдаёт depends_on; используем поле, если оно когда-либо появится.
    return Array.isArray(step.depends_on) ? step.depends_on.slice() : null;
  }

  /* ── Чипы (линейная навигация, доступная с клавиатуры) ──── */

  function renderChips(steps) {
    clear(els.stepList);
    if (!steps.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "У заказа нет шагов.";
      els.stepList.appendChild(li);
      return;
    }
    for (const s of steps) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.dataset.sid = s.id;
      btn.setAttribute("aria-current", "false");

      const name = document.createElement("span");
      name.textContent = s.id;
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.setAttribute("aria-hidden", "true");
      dot.textContent = " ";
      btn.appendChild(name);
      btn.appendChild(dot);
      btn.appendChild(stateNode(s.state));

      btn.addEventListener("click", () => selectStep(s.id));
      li.appendChild(btn);
      els.stepList.appendChild(li);
    }
  }

  /* ── DAG (SVG) ──────────────────────────────────────────── */

  function renderDag(steps) {
    clear(els.dag);
    els.dagNote.textContent = "";
    if (!steps.length) {
      els.dagNote.textContent = "Нет шагов для отображения.";
      return;
    }

    const hasDeps = steps.some((s) => {
      const d = stepDepends(s);
      return d && d.length > 0;
    });

    // Слои по глубине зависимостей; без depends_on — один вертикальный столбец.
    const byId = new Map(steps.map((s) => [s.id, s]));
    const depthCache = new Map();
    function depthOf(id, seen) {
      if (depthCache.has(id)) return depthCache.get(id);
      if (seen.has(id)) return 0; // защита от цикла
      seen.add(id);
      const deps = stepDepends(byId.get(id)) || [];
      let d = 0;
      for (const dep of deps) {
        if (byId.has(dep)) d = Math.max(d, depthOf(dep, seen) + 1);
      }
      seen.delete(id);
      depthCache.set(id, d);
      return d;
    }

    const columns = new Map();
    for (const s of steps) {
      const d = hasDeps ? depthOf(s.id, new Set()) : 0;
      if (!columns.has(d)) columns.set(d, []);
      columns.get(d).push(s);
    }

    const NODE_W = 190, NODE_H = 52, GAP_X = 70, GAP_Y = 18, PAD = 16;
    let maxRows = 1;
    for (const arr of columns.values()) maxRows = Math.max(maxRows, arr.length);
    const depthCount = Math.max(1, columns.size);
    const width = PAD * 2 + depthCount * NODE_W + (depthCount - 1) * GAP_X;
    const height = PAD * 2 + maxRows * NODE_H + (maxRows - 1) * GAP_Y;

    const svg = els.dag;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);

    // Маркер стрелки.
    const defs = document.createElementNS(NS, "defs");
    const marker = document.createElementNS(NS, "marker");
    marker.setAttribute("id", "arrow");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const arrow = document.createElementNS(NS, "path");
    arrow.setAttribute("d", "M0,0 L10,5 L0,10 z");
    arrow.setAttribute("fill", "var(--muted)");
    marker.appendChild(arrow);
    defs.appendChild(marker);
    svg.appendChild(defs);

    const pos = new Map();
    for (const [depth, arr] of columns) {
      const x = PAD + depth * (NODE_W + GAP_X);
      arr.forEach((s, row) => {
        const y = PAD + row * (NODE_H + GAP_Y);
        pos.set(s.id, { x, y, s });
      });
    }

    // Рёбра depends_on.
    const edges = document.createElementNS(NS, "g");
    edges.setAttribute("class", "edges");
    for (const s of steps) {
      const deps = stepDepends(s) || [];
      for (const dep of deps) {
        const a = pos.get(dep), b = pos.get(s.id);
        if (!a || !b) continue;
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
        const x2 = b.x, y2 = b.y + NODE_H / 2;
        const mx = (x1 + x2) / 2;
        const path = document.createElementNS(NS, "path");
        path.setAttribute("class", "dag-edge");
        path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
        edges.appendChild(path);
      }
    }
    svg.appendChild(edges);

    // Узлы: делаем их фокусируемыми и доступными через клавиатуру.
    const nodes = document.createElementNS(NS, "g");
    for (const { x, y, s } of pos.values()) {
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "dag-node s-" + (s.state || "pending"));
      g.setAttribute("transform", `translate(${x},${y})`);
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "button");
      g.setAttribute("aria-label", `Шаг ${s.id}, статус ${label(s.state)}`);
      g.setAttribute("aria-current", "false");
      g.dataset.sid = s.id;

      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("width", NODE_W);
      rect.setAttribute("height", NODE_H);
      rect.setAttribute("rx", "8");
      g.appendChild(rect);

      const name = document.createElementNS(NS, "text");
      name.setAttribute("x", "10");
      name.setAttribute("y", "21");
      name.textContent = truncate(s.id, 24);
      g.appendChild(name);

      const st = document.createElementNS(NS, "text");
      st.setAttribute("class", "node-state");
      st.setAttribute("x", "10");
      st.setAttribute("y", "39");
      st.textContent = `${label(s.state)}${s.type ? " · " + s.type : ""}`;
      g.appendChild(st);

      g.addEventListener("click", () => selectStep(s.id));
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectStep(s.id);
        }
      });
      nodes.appendChild(g);
    }
    svg.appendChild(nodes);

    if (!hasDeps) {
      els.dagNote.textContent = "REST API не отдаёт depends_on — показываю шаги без связей.";
    }
  }

  function truncate(s, n) {
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  /* ── Инспектор ──────────────────────────────────────────── */

  function selectStep(sid) {
    if (!view.order) return;
    view.selectedStep = sid;

    for (const el of els.dag.querySelectorAll(".dag-node")) {
      el.setAttribute("aria-current", el.dataset.sid === sid ? "true" : "false");
    }
    for (const btn of els.stepList.querySelectorAll(".chip")) {
      btn.setAttribute("aria-current", btn.dataset.sid === sid ? "true" : "false");
    }

    const step = (view.order.steps || []).find((s) => s.id === sid);
    showInspector(step || null);
    if (step) els.dag.setAttribute("aria-label", `Граф шагов; выбран ${sid}`);
  }

  function missingStub() {
    const span = document.createElement("span");
    span.className = "missing";
    span.textContent = "Недоступно через REST API";
    return span;
  }

  function showInspector(step) {
    if (!step) {
      els.inspEmpty.hidden = false;
      els.inspBody.hidden = true;
      return;
    }
    els.inspEmpty.hidden = true;
    els.inspBody.hidden = false;

    els.iId.textContent = step.id || "—";
    els.iType.textContent = step.type || "—";

    clear(els.iState);
    els.iState.appendChild(stateNode(step.state));

    els.iAttempts.textContent = step.attempts == null ? "—" : String(step.attempts);

    // depends_on — заглушка, если API не отдаёт.
    clear(els.iDeps);
    const deps = stepDepends(step);
    if (deps && deps.length) {
      els.iDeps.textContent = deps.join(", ");
    } else if (deps && deps.length === 0) {
      els.iDeps.textContent = "нет зависимостей";
    } else {
      els.iDeps.appendChild(missingStub());
    }

    // prompt — заглушка, если API не отдаёт.
    clear(els.iPrompt);
    if (typeof step.prompt === "string" && step.prompt.length) {
      els.iPrompt.textContent = step.prompt;
    } else {
      els.iPrompt.appendChild(missingStub());
    }

    els.iError.textContent = step.error_text || "";
    els.iLog.textContent = step.log_tail || "";
  }

  /* ── Инициализация ──────────────────────────────────────── */

  els.refresh.addEventListener("click", () => {
    loadOrders();
    if (view.order) loadOrder(view.order.id, { fromRoute: true });
  });

  window.addEventListener("hashchange", () => {
    const oid = hashOrderId();
    if (oid && (!view.order || view.order.id !== oid)) loadOrder(oid, { fromRoute: true });
  });

  loadOrders();
})();
