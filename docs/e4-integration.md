# E4: integration review (not deployed)

Inspected branch: `dirigent/ord_c23fbd1832a5-e4_planner_integration`.
Base HEAD and local dev: `6be2eee`; M1 planning entered at `d7d5213`.
The reported historic test counts were not independently reproduced.

Existing contracts reused:

* `Brain.plan(intent_text, repo) -> OrderSpec`, `Planner.propose(PlanningRequest)`
  and `Planner.approve(db, concrete_proposal)`; explicit approval is retained.
* M1 validates unique IDs, dependencies, cycles, step/replan/call limits,
  launch budget and explicit machine acceptance. SQLite stores the approved
  goal, DoD, policy, graph, route, attempts and conservative launch debits.
* Scheduler owns ready promotion, concurrency, execution timeout, acceptance,
  worktree handling, failover and recovery. No second execution loop is added.

## Gap found and adapter

M1's planned steps previously used direct executor lookup even inside
RegistryScheduler. The repository also contains two different registries:
public `src.registry.ExecutorRegistry` (`core.py`, select-only, `Selection.tag`)
and legacy `registry.registry.ExecutorRegistry` (multi-launch, `Selection.executor`).
The existing RegistryScheduler legacy path uses the latter. Its internal
failover could launch multiple attempts for one M1 debit, and its roles differ.

For planned orders only, RegistryScheduler now calls the public select-only
registry and returns the selected existing executor to M1's execution path.
Role translation: architect -> architecture, coder -> coding,
tester -> cheap_test, reviewer -> review. The candidate list contains only the
current approved route's backend; only M1 may advance to another approved route.
Selection is refreshed and audited as `planning.selection` in SQLite. No in-memory
registry attempt counters or multi-launch result cache replace durable M1 state.
Planner-provided `executor=registry` cannot enter the legacy multi-launch path
for a planned order. Legacy orders keep their existing path.

Tests inject `planning_registry=ExecutorRegistry(probers={...FakeProber...})`
and `executor_override` into RegistryScheduler. The normal daemon constructor
still works unchanged. No Telegram, web, MCP, production config or service
changes are required or included. The programmatic Planner API remains the entry
point; no new MCP/HTTP/free-text UI endpoint is claimed.

## Reproduction for Kimi

From this worktree, use the existing virtualenv:

```sh
/matrix/dirigent/.venv/bin/python -m pytest tests/test_e4_integration.py tests/test_planning.py tests/test_scheduler.py tests/test_registry.py tests/test_registry_scheduler.py -q
```

`test_goal_dag_registry_parallel_and_dependencies`: one free-form goal plus DoD,
policy and budget enters a fake Brain; its three-step graph has independent a/b
and join(a,b). Two distinct mock executor objects synchronize at a barrier,
proving simultaneous execution with max_parallel=2. Join checks both branches
finished. All three selections are checked in SQLite; the order must finish done.
E4 tests forbid socket connections and subprocess launches and use temporary DBs
and artifacts. They do not touch a target repository or call an external API.

`test_registry_budget_and_restart[1/2]`: primary fails with quota; close/reopen
SQLite and replace the scheduler/registry. With budget 1 the backup must never
launch; with budget 2 exactly one backup launch completes the order. Persisted
debits must equal the cap. Existing M1 tests cover planner refusal/replan bound,
cycle rejection, acceptance failure/cancellation, expired leases and call caps.

## Actual validation and remaining review

Passed in this environment:

```sh
/matrix/dirigent/.venv/bin/python -m pytest tests/test_planning.py -k 'reject_invalid or malformed or invalid_policy' -q
# 12 passed, 15 deselected
```

The async integration run hangs during the pre-existing `db` fixture, before
the E4 test body. A bounded diagnostic run dumps aiosqlite's worker waiting in
`_connection_worker_thread` and the event loop waiting in `selectors.select`
inside pytest's async fixture setup. It exits via `timeout` (124), not a test
assertion result. The unchanged M1 parallel test is checked separately with the
same bounded command. Do not count unexecuted async assertions as passed.

Kimi must reproduce the async suite in an environment where the existing DB
fixture completes, inspect the adapter and confirm production planner/backend
policy deliberately. Real CLI/LLM behavior and repository merge isolation are
not established by mock tests. No deployment or push was performed. No new commit
was created: this session's permission profile marks `.git` read-only; base SHA
above identifies the inspected revision, not these uncommitted changes.
