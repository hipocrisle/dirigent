"""P3a-2: тесты статического read-only интерфейса.

Проверяем раздачу статики через aiohttp, корректность основной разметки,
безопасность (нет innerHTML для пользовательских данных) и доступность
на уровне, который можно проверить без браузера/JS-движка.
"""
from __future__ import annotations

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.api import WEB_DIR, build_app


@pytest.fixture
async def client(db):
    app = build_app(db)
    async with TestClient(TestServer(app)) as cli:
        yield cli


# ── раздача статики ─────────────────────────────────────────

async def test_root_redirects_to_ui(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status in (302, 301)
    assert resp.headers["Location"].endswith("/ui/")


async def test_index_served(client):
    resp = await client.get("/ui/")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "<title>" in body


@pytest.mark.parametrize("name", ["app.css", "app.js"])
async def test_assets_served(client, name):
    resp = await client.get(f"/ui/{name}")
    assert resp.status == 200
    ct = resp.headers["Content-Type"]
    if name.endswith(".css"):
        assert "text/css" in ct
    else:
        assert "javascript" in ct or "text/plain" in ct
    await resp.read()


async def test_static_files_exist_on_disk():
    for name in ("index.html", "app.css", "app.js"):
        assert (WEB_DIR / name).is_file(), f"нет {name}"


async def test_missing_static_returns_404(client):
    resp = await client.get("/ui/nope.js")
    assert resp.status == 404


async def test_static_post_not_allowed(client):
    # Статика монтируется GET-only; POST не должен попадать в файловый обработчик.
    resp = await client.post("/ui/app.js")
    assert resp.status in (404, 405)


# ── основная разметка ───────────────────────────────────────

async def test_index_has_core_elements(client):
    resp = await client.get("/ui/")
    body = await resp.text()
    assert 'id="orders"' in body
    assert 'id="detail"' in body
    assert 'id="dag"' in body
    assert "<svg" in body
    assert 'id="insp-body"' in body


async def test_index_references_only_local_assets(client):
    body = await (await client.get("/ui/")).text()
    assert 'href="./app.css"' in body
    assert 'src="./app.js"' in body
    # Никаких внешних CDN/шрифтов — статика самодостаточна.
    assert "http://" not in body
    assert "https://" not in body


# ── безопасность ────────────────────────────────────────────

async def test_js_does_not_use_innerhtml_on_user_data():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Присваивание/запись innerHTML запрещены во всём клиенте (строим DOM точечно).
    assert not re.search(r"\.innerHTML\s*=", js)
    assert not re.search(r"\.outerHTML\s*=", js)
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    # Текст пользовательских полей ставится через textContent.
    assert "textContent" in js


async def test_js_only_get_requests():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'method: "GET"' in js
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        assert verb not in js, f"клиент не должен слать {verb}"


async def test_index_has_no_inline_handlers(client):
    body = await (await client.get("/ui/")).text()
    # Никаких inline-обработчиков событий (лучше для CSP и безопасности).
    assert not re.search(r"\son\w+\s*=", body, re.IGNORECASE)
    assert "javascript:" not in body.lower()


# ── доступность (проверяемое без браузера) ──────────────────

async def test_accessibility_structure(client):
    body = await (await client.get("/ui/")).text()
    assert 'lang="ru"' in body
    assert 'class="skip-link"' in body          # пропуск к контенту
    assert 'id="main"' in body
    assert 'aria-live="polite"' in body          # объявления статуса
    assert 'role="status"' in body
    assert 'aria-label="Список заказов"' in body
    assert 'aria-label="Навигация по шагам"' in body
    assert "<noscript>" in body                  # запасной вариант без JS


async def test_js_keyboard_support():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Узлы графа фокусируемы и обрабатывают Enter/Space.
    assert 'setAttribute("tabindex", "0")' in js
    assert 'ev.key === "Enter"' in js
    assert 'ev.key === " "' in js
    assert 'aria-current' in js


async def test_js_handles_loading_empty_error_states():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "Загрузка заказов" in js
    assert "Заказов нет" in js
    assert "Ошибка загрузки" in js
    assert "Заказ не найден" in js


async def test_js_stubs_missing_fields():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Если API не отдаёт prompt/depends_on — честная заглушка, а не выдумка.
    assert "Недоступно через REST API" in js
    # depends_on используется, если появится; при отсутствии — честное предупреждение.
    assert "depends_on" in js
    assert "не отдаёт depends_on" in js


async def test_api_detail_lacks_prompt_and_depends_on_rn():
    """Фиксируем известный пробел REST API (не расширяем бэкенд на P3a-2).

    Если это начнёт падать — значит API начал отдавать поля; тогда заглушку
    во фронтенде можно убрать (но она не мешает).
    """
    from src.api import _step_public

    class Row(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    row = Row(id="s1", state="ready", type="mock.ok", attempts=0,
              error_class=None, error_text=None, result_json=None)
    pub = _step_public(row)
    assert "prompt" not in pub
    assert "depends_on" not in pub


# ── совместимость: статика не ломает API/SSE ────────────────

async def test_api_orders_still_works(client):
    resp = await client.get("/orders")
    assert resp.status == 200
    assert isinstance(await resp.json(), list)


async def test_static_js_uses_absolute_api_path():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Страница в /ui/, API в корне — относительный "orders" дал бы /ui/orders (404).
    assert 'const API = "/orders"' in js


async def test_order_detail_shape_with_real_order(client, db):
    """Реальный заказ: деталка отдаёт поля, нужные визуализатору."""
    import yaml

    from src.models import OrderSpec
    from src.states import OrderState

    spec = OrderSpec.model_validate(yaml.safe_load(yaml.safe_dump(
        {"goal": "web", "steps": [{"id": "a", "type": "mock.ok"},
                                   {"id": "b", "type": "mock.ok", "depends_on": ["a"]}]},
        allow_unicode=True)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)

    lst = await (await client.get("/orders")).json()
    assert any(o["id"] == oid and "progress" in o for o in lst)

    det = await (await client.get(f"/orders/{oid}")).json()
    assert det["id"] == oid
    assert len(det["steps"]) == 2
    for s in det["steps"]:
        for key in ("id", "state", "type", "attempts"):
            assert key in s


async def test_order_detail_404(client):
    resp = await client.get("/orders/ord_missing")
    assert resp.status == 404
    assert (await resp.json())["error"] == "not found"


async def test_sse_route_intact(client):
    # Маршрут SSE существует и не перекрыт статикой. Реальный поток
    # тестируется в test_sse_events.py — здесь только маршрутизация.
    resp = await client.get("/api/v1/events")
    assert resp.status == 200
    assert "text/event-stream" in resp.headers["Content-Type"]
    await resp.release()
