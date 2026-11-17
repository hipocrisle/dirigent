# Automatic planning and assignment

`src.planning` uses the existing `Brain.plan(intent_text, repo) -> OrderSpec`
interface. Set `DIRIGENT_BRAIN=cli` and `DIRIGENT_PLANNER_CLI` to an argv
command for an already installed, authorized CLI. The adapter sends the request
on stdin and expects only OrderSpec JSON on stdout (120 second timeout, nonzero
exit rejected). It introduces no API client or paid service. Backend-specific CLI
flags belong in this environment variable; use a small wrapper if the CLI does
not accept stdin. Static and Kimi brains remain available.

```python
from src.planning import Planner, PlanningRequest

request = PlanningRequest.model_validate({
    'goal': 'Implement the requested feature',
    'definition_of_done': ['Tests pass', 'Review artifact exists'],
    'budget': 0.25,
    'launch_cost': 0.01,
    'policy': {
        'max_steps': 16, 'max_replans': 2, 'max_calls': 24,
        'failover_on': ['quota', 'resource', 'network'],
        'routes': {
            role: [{'role': role, 'backend': 'codex'},
                   {'role': role, 'backend': 'opencode'}]
            for role in ('architect', 'coder', 'tester', 'reviewer')
        }
    }
})
planner = Planner()
proposal = planner.propose(request, repo='/path/to/repo')
# Present proposal.model_dump_json(indent=2) to the approving caller.
# Only after approval of this concrete plan:
order_id = await planner.approve(db, proposal)
# The existing running Scheduler(db, max_parallel=2) picks it up automatically.
```

Each generated step specifies `inputs.role`, a nonempty prompt, dependencies,
and nonempty `acceptance.checks`. The validator checks unique IDs, known
dependencies, acyclicity, bounded step count, timeout <=3600 seconds, supported
check names and required checker parameters. Assignment backend, launch cost,
retry and approval settings are determined by the caller policy, never by the
model. Roles may switch only along the ordered routes approved for the original
role (up to three distinct routes). Definition of done is sent to the planner
and retained in the approved SQLite snapshot; semantic adequacy of the concrete
checks is part of plan approval, not something a schema validator can prove.
At most two corrective replans occur before approval. There is no runtime
rewrite of an approved graph and no perpetual planner conversation.

## Scheduler contract

The existing scheduler is the only execution loop; run **one scheduler owner per
SQLite database**, with `max_parallel=2`. Existing DAG promotion, leases,
acceptance execution, dependency cancellation and state transitions are reused.
The new hooks apply only to orders containing `constraints.planning`:

* Before an execution attempt, reserve a durable debit. A single conditional
  SQLite INSERT checks both the launch count and the total reservation budget.
* Executors report blocked/offline resources as `ErrorClass.resource` (the existing
  state machine has no separate blocked error class). Resource absence and failed results consult the original role's ordered
  routes. The next route's role and executor type are saved to the step snapshot
  before the legal `running -> retry_scheduled -> ready` transition.
* Quota never enters the old indefinite quota polling loop for planned orders.
  Once policy routes or budget are exhausted the step fails, allowing the normal
  order error/cascade mechanism to escalate. Non-policy failures fail immediately.
* Restart uses existing recovery and persisted attempts/spec/debits. Reservations
  are deliberately not refunded after a crash: uncertain work is not assumed free.

`launch_cost` is an approved conservative bound per attempt, including offline
probes and retries. Set it to cover the backend's worst-case permitted run. For
unmetered subscription CLIs, `max_calls` provides an additional finite resource
budget. This is admission control, not a billing guarantee: a CLI which ignores
its external spending limits cannot be prevented from overspending by an
orchestrator after launch. Planner calls are bounded separately (1 + max_replans)
and run under the installed CLI's existing quota, before execution approval.
No successful attempt is accepted without the existing machine checkers.

Integration tests: `python -m pytest tests/test_planning.py tests/test_scheduler.py -q`.
They exercise three independent branches and a join, concurrency two, policy
failover, exhausted paths, budget admission and SQLite reopen/recovery, plus the
real subprocess CLI adapter with a local deterministic planner stub.

Lost running leases in planned orders are handled by Assignment before legacy DB
recovery: the uncertain execution is classified as `network`, and consumes its
route. Both startup recovery and lease expiry during ticks advance to the next
approved route, or fail if network failover is forbidden/exhausted. This uses
legal state-machine transitions and retains every previous budget debit.
