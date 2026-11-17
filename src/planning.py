"""Validated planning and durable, bounded assignment for the existing scheduler."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import OrderSpec, StepResult
from .states import OrderState, StepState

Role = Literal['architect', 'coder', 'tester', 'reviewer']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Route(Strict):
    role: Role
    backend: str = Field(min_length=1, pattern=r'^[a-zA-Z0-9_-]+$')


class PlanningPolicy(Strict):
    routes: dict[Role, list[Route]]
    max_steps: int = Field(default=24, ge=1, le=100)
    max_replans: int = Field(default=2, ge=0, le=2)
    max_calls: int = Field(default=48, ge=1, le=200)
    failover_on: list[Literal['quota', 'resource', 'network', 'auth', 'runtime', 'timeout', 'unknown']] = ['quota', 'resource', 'network']

    @model_validator(mode='after')
    def check_routes(self):
        if not self.routes:
            raise ValueError('approved role routes required')
        for role, routes in self.routes.items():
            if not 1 <= len(routes) <= 3 or routes[0].role != role:
                raise ValueError('role needs 1..3 approved routes, starting with itself')
            if len({(r.role, r.backend) for r in routes}) != len(routes):
                raise ValueError('duplicate assignment route')
        return self


class PlanningRequest(Strict):
    goal: str = Field(min_length=1)
    definition_of_done: list[str] = Field(min_length=1)
    budget: float = Field(ge=0)
    # Conservative debit per launch, including unknown usage. No assumed free calls.
    launch_cost: float = Field(gt=0)
    policy: PlanningPolicy


def validate_plan(spec: OrderSpec, request: PlanningRequest) -> OrderSpec:
    spec = OrderSpec.model_validate(spec.model_dump())
    if not 0 < len(spec.steps) <= request.policy.max_steps:
        raise ValueError('step count exceeds policy')
    seen, visiting = set(), set()
    by_id = {s.id: s for s in spec.steps}

    def visit(sid):
        if sid in visiting:
            raise ValueError('cyclic DAG')
        if sid in seen:
            return
        visiting.add(sid)
        for dep in by_id[sid].depends_on:
            visit(dep)
        visiting.remove(sid)
        seen.add(sid)

    for step in spec.steps:
        visit(step.id)
        role = step.inputs.get('role')
        if role not in ('architect', 'coder', 'tester', 'reviewer'):
            raise ValueError('unknown step role')
        routes = request.policy.routes.get(role, [])
        if not routes or len(routes) > 3 or routes[0].role != role:
            raise ValueError('role needs 1..3 approved routes, starting with itself')
        if len({(r.role, r.backend) for r in routes}) != len(routes):
            raise ValueError('duplicate assignment route')
        checks = step.acceptance.get('checks')
        if not isinstance(checks, list) or not checks or any(
                c not in ('pytest', 'go_test', 'repo_changed', 'files_exist', 'artifact', 'diff_apply') for c in checks):
            raise ValueError('machine-checkable acceptance required')
        files = step.acceptance.get('expect_files')
        if 'files_exist' in checks and (not isinstance(files, list) or not files or
                                       any(not isinstance(p, str) or not p.strip() for p in files)):
            raise ValueError('expect_files required')
        if 'artifact' in checks and not step.outputs:
            raise ValueError('outputs required')
        if 'diff_apply' in checks and not step.inputs.get('diff'):
            raise ValueError('diff required')
        if not step.prompt.strip() or not 0 < step.timeout_seconds <= 3600:
            raise ValueError('bounded small step required')
        step.type = routes[0].backend
        step.retry.max_attempts = len(routes)
        step.retry.backoff_seconds = 0
        step.approval.mode = 'none'
        step.budget_usd = request.launch_cost
    if len(spec.steps) > request.policy.max_calls or len(spec.steps) * request.launch_cost > request.budget:
        raise ValueError('plan cannot fit launch budget')
    spec.goal = request.goal
    spec.budget_max_usd = request.budget
    spec.constraints = {'planning': request.model_dump()}
    return spec


class CLIBrain:
    """Replaceable argv CLI; prompt on stdin, OrderSpec JSON on stdout; no shell/API."""
    def __init__(self, argv=None):
        self.argv = argv or shlex.split(os.environ.get('DIRIGENT_PLANNER_CLI', ''))

    def plan(self, intent_text: str, repo: str | None = None) -> OrderSpec:
        if not self.argv:
            raise ValueError('DIRIGENT_PLANNER_CLI is required')
        prompt = ('Return only OrderSpec JSON: goal, steps [{id,type (use any string; policy overrides it),prompt,inputs:{role: '
                  'architect|coder|tester|reviewer},depends_on:[],acceptance:{checks:[]}}]. '
                  'Use small bounded steps, an acyclic graph, and concrete supported acceptance '
                  'checks pytest/go_test/repo_changed/files_exist (+expect_files)/artifact (+outputs). '
                  'Honor definition_of_done and policy.\n' + intent_text)
        result = subprocess.run(self.argv, input=prompt, text=True, capture_output=True,
                                timeout=120, check=True)
        if len(result.stdout) > 1_000_000:
            raise ValueError('planner output too large')
        spec = OrderSpec.model_validate_json(result.stdout)
        spec.repo = repo
        return spec


class Planner:
    def __init__(self, brain=None):
        from .brain import get_brain
        self.brain = brain or get_brain()

    def propose(self, request: PlanningRequest, repo=None) -> OrderSpec:
        error = ''
        for _ in range(request.policy.max_replans + 1):
            try:
                spec = self.brain.plan(request.model_dump_json() + error, repo)
                return validate_plan(spec, request)
            except (ValueError, subprocess.SubprocessError, OSError) as exc:
                error = '\nPrevious plan rejected: ' + str(exc)[:1000]
        raise ValueError('planning routes exhausted: ' + error)

    async def approve(self, db, proposal: OrderSpec) -> str:
        # Approval applies to this concrete snapshot. Revalidate before persistence.
        spec = proposal.model_copy(deep=True)
        request = PlanningRequest.model_validate(spec.constraints['planning'])
        spec = validate_plan(spec, request)
        oid = await db.create_order(spec)
        await db.transition_order(oid, OrderState.active)
        return oid


class Assignment:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    async def request(self, oid):
        row = await (await self.db.conn.execute('SELECT spec_json FROM orders WHERE id=?', (oid,))).fetchone()
        data = json.loads(row['spec_json']).get('constraints', {}).get('planning')
        return PlanningRequest.model_validate(data) if data else None

    async def recover(self, *, all_running=False):
        """Lost executions consume their route; never silently rerun an offline CLI.

        Startup assumes the documented single scheduler owner. Tick recovery only
        handles expired leases. Legacy orders retain the existing DB recovery.
        """
        from .models import now
        rows = await (await self.db.conn.execute(
            "SELECT order_id, id FROM steps WHERE state='running' "
            "AND (? OR lease_until < ?)", (all_running, now()))).fetchall()
        for row in rows:
            if await self.request(row['order_id']) is not None:
                await self.failed(row['order_id'], row['id'], StepResult(
                    ok=False, error_class='network', error_text='execution lease lost'))

    async def reserve(self, oid, sid):
        request = await self.request(oid)
        if request is None:
            return True
        async with self.lock:
            await self.db.conn.execute('CREATE TABLE IF NOT EXISTS planning_debits (order_id TEXT, step_id TEXT, cost REAL)')
            debit = await self.db.conn.execute(
                'INSERT INTO planning_debits(order_id,step_id,cost) '
                'SELECT ?,?,? WHERE '
                '(SELECT COUNT(*) FROM planning_debits WHERE order_id=?) < ? AND '
                '(SELECT COALESCE(SUM(cost),0) FROM planning_debits WHERE order_id=?) + ? <= ?',
                (oid, sid, request.launch_cost, oid, request.policy.max_calls,
                 oid, request.launch_cost, request.budget + 1e-9))
            await self.db.conn.commit()
            if debit.rowcount != 1:
                await self.db.transition_step(oid, sid, StepState.failed, error_class='resource',
                                              error_text='approved launch budget exhausted', lease_owner=None, lease_until=None)
                await self.db.event('planning.exhausted', oid, sid, {'reason': 'budget'})
                return False
        return True

    async def failed(self, oid, sid, result: StepResult):
        request = await self.request(oid)
        if request is None:
            return False
        spec = await self.db.get_step_spec(oid, sid)
        original = OrderSpec.model_validate_json((await (await self.db.conn.execute(
            'SELECT spec_json FROM orders WHERE id=?', (oid,))).fetchone())['spec_json'])
        role = next(s.inputs['role'] for s in original.steps if s.id == sid)
        routes = request.policy.routes[role]
        row = await (await self.db.conn.execute('SELECT attempts FROM steps WHERE order_id=? AND id=?', (oid, sid))).fetchone()
        index = row['attempts']
        if result.error_class.value in request.policy.failover_on and index < len(routes):
            route = routes[index]
            spec.type = route.backend
            spec.inputs['role'] = route.role
            await self.db.transition_step(oid, sid, StepState.retry_scheduled,
                spec_json=spec.model_dump_json(), type=spec.type, backoff_until=0,
                lease_owner=None, lease_until=None, result_json=result.model_dump_json())
            await self.db.event('planning.failover', oid, sid, route.model_dump())
        else:
            await self.db.transition_step(oid, sid, StepState.failed, error_class=result.error_class.value,
                error_text=result.error_text, result_json=result.model_dump_json(), lease_owner=None, lease_until=None)
            await self.db.event('planning.exhausted', oid, sid, {'reason': 'routes'})
        return True
