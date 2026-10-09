# Doris HTTP Reliability Lab

A small, executable case study for developers building database-backed agents.
It demonstrates a confirmed timeout defect, a retry behavior that should not
be called a defect, and a timeout-scope question that needs a design decision.

This independent educational project is not an Apache project or certification.
It does not claim a new vulnerability, Apache membership, endorsement, or human
review. Analysis, code, tests, and documentation were assisted by OpenAI Codex
(GPT-6). The lab is licensed under Apache-2.0; see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Run

Prerequisites: **CPython 3.12 on Linux x86_64 with glibc 2.17+**, with
`venv`/`ensurepip`, and access
to public PyPI and `raw.githubusercontent.com`. From this directory:

```bash
python3 lab.py
```

The command creates a local `.lab/venv`, installs ten fixed binary-wheel
dependencies with SHA-256 verification, checks their dependency consistency,
downloads one pinned public upstream module, verifies its SHA-256, then runs
six cases and four guard tests. It does not install anything into the system
Python, change production settings, start a database, or require credentials.
Downloads are setup operations; the experiment phase permits connections
only to **127.0.0.1**, with an additional socket guard. It makes no real DNS
queries: controlled stubs supply loopback answers. Metadata addresses occur
only as synthetic data that the client must reject.

Setup failures, unsupported environments, mismatched hashes, or test failures
exit nonzero. There is no silent mirror/cache fallback. A previously downloaded
module is reused only after rechecking its hash. The environment supports the
fixed platform above; other Python/platform combinations have not been tested.

## What the cases establish

| Case | Evidence | Classification |
|---|---|---|
| `nan_red_green` | Baseline keeps NaN and accepts a delayed response; the minimal runtime repair uses a finite fallback and times out. | Confirmed defect covered by existing [Doris PR #239](https://github.com/apache/doris-mcp-server/pull/239). |
| `per_fe_budget` | Two 80ms HTTP attempts finish successfully even though their combined time exceeds the 120ms per-attempt total. | Expected per-request retry behavior; no shared failover deadline is promised. |
| `dns_plus_http` | A 90ms DNS preflight plus 30ms HTTP succeeds with a 50ms HTTP total; there is exactly one DNS lookup and the address is pinned. | Design question: should the total include preflight? |
| `dns_connect_bound` | A stalled synthetic resolver is cancelled after the 60ms connect budget; no HTTP resources are created. | Evidence against an indefinitely blocked caller for these finite inputs. |
| `native_dns_cancel` | Caller cancellation propagates while an executor resolver worker is still blocked; the lab explicitly releases the worker and observes completion. | Distinguishes caller cancellation from interruption of a running resolver thread. |
| `mixed_dns_rejected` | A mixed loopback/metadata answer is rejected before sessions, connectors, or TCP requests. | SSRF boundary remains intact. |

The NaN case uses the same acceleration as the original regression: both
in-memory module copies have their default total fallback temporarily shortened
from **30 seconds to 40ms**. The loopback response is delayed 120ms. This makes
the NaN baseline produce an expected RED condition and the repaired copy a GREEN
condition quickly. It does not claim the production default is 40ms. Only the
runtime normalizer repair is demonstrated here; the PR also repairs configuration
validation, which this minimal lab does not load or retest.

The cause is a numeric boundary check: comparisons such as `NaN <= 0` are false,
so the original normalizer lets NaN through. A finite-number check prevents that
invalid value from disabling `aiohttp`'s total timer; independent read/connect
budgets may still limit a request, so a missing total timer alone is not proof
of an infinite wait.

The FE case uses **two logical `.invalid` hostnames on one loopback listener**,
with Host-based responses. Each hostname is explicitly allowlisted and resolves
to 127.0.0.1. This exercises the actual ordered failover implementation without
contacting a cluster; it does not simulate independent physical FE failures.
The original feature describes [ordered FE retries in PR #163](https://github.com/apache/doris-mcp-server/pull/163).

## Output and interpretation

Each run writes `.lab/results.json`, including dependency versions, source
hashes, classifications, timings, and observed resource cleanup. A real cloud
run is included in [example-results.json](example-results.json), with its
corresponding [case output](example-output.txt). Timings vary with scheduling.

```text
PASS nan_red_green: confirmed defect; existing PR #239
PASS per_fe_budget: expected per-request retry behavior
PASS dns_plus_http: budget-scope design question
PASS dns_connect_bound: infinite-caller-wait hypothesis disproved
PASS native_dns_cancel: bounded caller; resolver worker can continue
PASS mixed_dns_rejected: SSRF boundary preserved
DONE: 6 cases and 4 guard tests passed
```

The complete program also prints measured milliseconds and setup progress.
RED is an expected baseline condition, not a failing overall lab run. GREEN
means the demonstrated repair passed this bounded case; it does not mean every
production path was tested. Actual `aiohttp` sessions/connectors are tracked,
their `.closed` state is checked, and HTTP peer EOF is observed. The listener,
handler tasks, and controlled resolver worker are closed before completion.

Every client operation has a one-second harness watchdog; server tasks have
one-second guards; the controlled blocking resolver has a one-second safety
bound. The parent bounds the experiment process to 30 seconds. Setup stages
also have independent process limits: venv 60s, package install 180s, dependency
check 20s, source download 45s, and guard tests 20s. These watchdogs make a broken
case fail rather than silently wait forever; they are not product timeout fixes.

## Sources and limits

Baseline: public upstream commit
[`5daf1deb26bc0db02c19bf5ca1d070acea4cfab9`](https://github.com/apache/doris-mcp-server/commit/5daf1deb26bc0db02c19bf5ca1d070acea4cfab9).
The runner downloads only
[`doris_http_client.py`](https://github.com/apache/doris-mcp-server/blob/5daf1deb26bc0db02c19bf5ca1d070acea4cfab9/doris_mcp_server/utils/doris_http_client.py),
retaining its original Apache header. Dependency versions and wheel hashes
come from that commit's [public lockfile](https://github.com/apache/doris-mcp-server/blob/5daf1deb26bc0db02c19bf5ca1d070acea4cfab9/uv.lock).
The repair adds `math` and a finite-number check to `_bounded_float` in memory.
Its bytes before the modification notice match the previously tested runtime
repair exactly, with a second pinned SHA-256. No upstream tests are copied.

The [configuration table](https://github.com/apache/doris-mcp-server/blob/5daf1deb26bc0db02c19bf5ca1d070acea4cfab9/docs/reference/configuration.md#L62)
says "total request timeout". DNS preflight is independently bounded by connect
timeout and precedes the HTTP total timer. The actionable question is whether
that label should cover an entire endpoint attempt starting before preflight,
or just the HTTP exchange after validation. This lab does not silently change
that contract or classify the ambiguity as a new vulnerability.

Not measured: real Doris, OS DNS server behavior, TLS, operating-system resolver
eventual completion, executor exhaustion under concurrency, production gateways,
or all MCP workflows. Cancellation of the controlled native worker is not proof
that every OS resolver can be interrupted. At longer timeouts `aiohttp` may round
expiry to the next second; the sub-second cases avoid that rounding. Event-loop
scheduling still means measured timings are not hard real-time guarantees.

Earlier regression artifacts were verified against their saved archives and
hashes before creating this lab. The project publishes newly generated results,
not private reports or raw logs. [evidence-provenance.json](evidence-provenance.json)
records the limited historical checks; full Doris release gates are not rerun
or claimed by this small project. PR #239 was open and unmerged when checked on
2026-10-09; its later status does not change the pinned baseline experiment.
