from src.states import OrderState, StepState, can_order, can_step


def test_allowed_basic():
    assert can_step(StepState.pending, StepState.ready)
    assert can_step(StepState.ready, StepState.running)
    assert can_step(StepState.running, StepState.completed)
    assert can_step(StepState.running, StepState.waiting_quota)
    assert can_step(StepState.waiting_quota, StepState.ready)
    assert can_step(StepState.running, StepState.waiting_approval)
    assert can_step(StepState.waiting_approval, StepState.ready)
    assert can_step(StepState.waiting_approval, StepState.cancelled)


def test_forbidden():
    assert not can_step(StepState.pending, StepState.running)      # скачок
    assert not can_step(StepState.completed, StepState.ready)      # терминал
    assert not can_step(StepState.failed, StepState.completed)     # терминал
    assert not can_step(StepState.ready, StepState.pending)        # назад нельзя
    assert not can_step(StepState.running, StepState.ready)        # lease не отпускаем просто так


def test_terminal_locked():
    for t in (StepState.completed, StepState.failed, StepState.cancelled):
        for b in StepState:
            assert not can_step(t, b)


def test_order_transitions():
    assert can_order(OrderState.new, OrderState.active)
    assert can_order(OrderState.active, OrderState.waiting_human)
    assert can_order(OrderState.waiting_human, OrderState.active)
    assert can_order(OrderState.active, OrderState.done)
    assert can_order(OrderState.error, OrderState.active)   # ручной рестарт
    assert not can_order(OrderState.done, OrderState.active)
    assert not can_order(OrderState.new, OrderState.done)   # скачок запрещён
