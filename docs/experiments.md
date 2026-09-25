# YAML experiments and evaluation datasets

The evaluator is a separate headless experimentation application, launched with
`cyber-agent-flow-eval` from its own project and environment. `engine.path` selects
the external CAF checkout, and optional `engine.python` selects its worker interpreter.
The main tool handles interactive sessions, analysis, generation and artifact tests;
the evaluator consumes prepared tasks and selected artifacts. It reuses the existing
`MCPSession` engine in a fresh worker process
for each attempt. It does not import Flask, automate the WebUI, or change the
repository's `kali_tools.json`. This is the first evaluation foundation described
in [the research plan](../../cyber-agent-flow/experiment-ideas/generated-artifact-evaluation-research-plan.md).
It supports supplied-evidence studies and ScenarioForge packages, including explicit
discovery tasks. Deployment, restoration and artifact generation remain separate.
Start with the [end-to-end workflow](artifact-evaluation-workflow.md) for the handoff
between the main application, ScenarioForge and this evaluator.

## Quick start

Install this project as described in [README](../README.md) (Python 3.10+, Linux/macOS).
Set `engine.path` and `engine.python` in the YAML before planning or running.
From the cyber-agent-flow-eval repository root:

```bash
# Validate YAML, read/freeze referenced input contents in memory, print schedule.
# Does not contact a model, launch tools, or create a dataset.
.venv/bin/cyber-agent-flow-eval plan configs/experiments/example.yaml

# Set model.name in the example to an installed model; start its backend first.
.venv/bin/cyber-agent-flow-eval run configs/experiments/example.yaml \
  --output eval-runs/inventory-development

# Continue using the identical YAML, referenced contents, engine, and dependencies.
.venv/bin/cyber-agent-flow-eval run configs/experiments/example.yaml \
  --output eval-runs/inventory-development --resume

# Explicitly add attempts for unsuccessful or failed trials; preserve old attempts.
.venv/bin/cyber-agent-flow-eval run configs/experiments/example.yaml \
  --output eval-runs/inventory-development --resume --retry-failed

# Rebuild exports from saved attempts; no execution.
.venv/bin/cyber-agent-flow-eval export eval-runs/inventory-development
```

The example creates four trials: one development task × two conditions × two
repetitions. It compares baseline with guidance, using supplied observations and
no model-callable tools. It checks a JSON inventory against an expected answer.
This is a pipeline example, not evidence of artifact efficacy or a held-out study.

Exit codes: `0` means all latest attempts executed to completion (verifier failures
can still occur); `1` means an execution did not complete; `2` means configuration
or setup failed; `130` means interrupted. Inspect `verified_success` for task
outcomes rather than using process exit code as the research endpoint.

## YAML contract (version 1)

Evaluation sessions default to `execution.reveal_network_policy: false`. The full
`execution.network_policy` still reaches the enforcement code, but its allow/disallow
lists are omitted from initial and rebuilt model system prompts. Policy denial
messages do not quote rules. Ordinary interactive sessions retain their existing
policy display. Non-discovery evaluations can explicitly set the field to `true`.

ScenarioForge version 3 supports authored discovery tasks. Their starting facts
form the participant briefing; discoverable fact values, evidence locations,
dependencies and objective mappings stay in evaluator metadata. Import preserves
CAF's scope and checks known objective addresses against it. Discovery suites reject
`reveal_network_policy: true` and literal hidden facts/hidden-subnet IPv4 addresses
in participant tasks or condition catalogs/guidance. Use the same starting facts
and scenario evidence in every artifact condition.

Discovery flag tasks use `flags_found`: the agent returns
`{"flags":["FLAG{recovered}"]}` without needing private objective IDs or target IPs.
The evaluator scores each expected flag, rejects duplicates/unknown flags for full
success, and keeps raw expected values out of the scoring checks. It does not claim
to measure whether a specific clue was read. Non-discovery tasks keep `flags_match`.

The checks prevent direct prompt disclosure, not all possible information access.
Private policy still exists in worker configuration/environment for enforcement;
same-user tools can potentially read local files or process state. Use external
isolation for strict ground-truth secrecy. Scope decisions can also reveal whether
a submitted target is permitted; no blind authorization oracle is claimed.

See the complete [example](../configs/experiments/example.yaml). Unknown fields,
duplicate YAML keys, duplicate task/condition IDs, invalid budgets, and unsupported
verifier types are rejected. Paths in YAML are relative to the YAML file.
IDs use letters, digits, underscores, and hyphens, beginning with a letter or digit.

| Field | Meaning |
| --- | --- |
| `version` | Must be `1` |
| `engine.path` | Required CAF checkout directory; absolute or relative to this YAML |
| `engine.python` | Optional executable path, relative to this YAML; defaults to evaluator Python |
| `id`, optional `description` | Experiment identity and description |
| `model.provider` | `ollama_direct`, `openai`, `litellm`, or `claude` |
| `model.url`, `model.name` | Provider endpoint and model name |
| `model.api_key_env` | Optional environment variable name; never put the key in YAML |
| `model.ssl_verify` | Boolean; defaults to true |
| `execution.wall_seconds` | Whole worker deadline including startup, inference, tools, and shutdown |
| `execution.max_turns` | Engine turn limit; exhaustion is recorded separately |
| `execution.tool_timeout` | Native tool timeout/checkpoint interval; defaults to 60 |
| `execution.context_window` | Engine context budget; defaults to 8192 |
| `execution.network_policy` | Required explicit `allow` and `disallow` lists |
| `execution.reveal_network_policy` | Boolean, defaults to false; discovery suites require false |
| `execution.target_lock` | Shared advisory lock file for coordinators using the same target |
| `repetitions` | Positive integer, defaults to 1; repetitions numbered from 0 |
| `order_seed` | Integer randomizing condition order within each task/repetition block |
| `tasks` | Nonempty task list |
| `conditions` | Nonempty condition list |

A task requires `id`, `family`, `split` (`development`, `validation`, or `test`),
`scenario_id`, `prompt`, and `verifier`. Imported suites supply these fields instead of inline tasks. Family and scenario labels are recorded;
the runner does not validate your partition design or scenario deployment.

A condition requires `id`, `catalog` (native MCP JSON catalog path), and `tools`
(an exact list of callable names; `[]` explicitly enables no tools). Optional
`guidance_files` contains UTF-8 document paths, concatenated in listed order.
Guidance is injected in the system prompt without the interactive guide loader's
truncation. Normal context management can subsequently summarize the conversation;
check the saved model requests to see what the model actually received.

Tool selection and guidance selection are independent. A four-condition study can
use baseline tools for A/B and baseline plus the generated tool for C/D, with
`guidance_files` present only for B/D. Necessary tool interface descriptions remain
in C. Unavailable selected tools cause execution failure. Model-requested tools
outside the list are blocked before dispatch, including direct engine calls.
Native built-ins must also be explicitly selected to be callable.

The native `mcp_kali.py` server is used. Catalogs use the existing native tool
format; generated executables must already be installed and referenced correctly
by the catalog. Catalog JSON and guidance contents are frozen in the manifest.
**Executable files, containers, model weights, and remote targets are not copied or
frozen automatically.** Pin and archive those dependencies separately before making
reproducibility claims. The artifact hash describes catalog, guidance, and selection,
not the transitive executable dependency graph.

`order_seed` is not a model seed. Version 1 rejects model seed, temperature, token
budget, custom server, and other unsupported YAML options rather than silently
ignoring them. Model generation uses the shared engine's existing provider defaults.
Matched settings and order do not guarantee identical model outputs.

## Verifiers

Verifiers execute in the coordinator after successful worker execution. Their
expected values are excluded from worker input and model requests.

* `json_equals`: parse the final answer as JSON, then compare canonical JSON with
  `expected`. Object key order does not matter; array order and JSON value types do.
  Markdown fences, extra prose, and invalid JSON fail.
* `contains_all`: check every literal, case-sensitive string in a nonempty
  `expected` list. Use for smoke tests; matching phrases does not establish factual
  correctness or evidence validity.

* `flags_match`: exact objective-to-flag checks with a partial completion score; used by ScenarioForge flag tasks.
* `flags_found`: match a JSON flags array to private objectives, without exposing
  objective IDs or addresses; used by authored discovery tasks. Duplicates and
  unknown flags prevent full success.

Each evaluation includes version, definition hash, individual checks, and references
to `result.json` and `messages.json`. These initial verifiers assess final answers;
they do not independently probe live network state. Domain-specific environment
verifiers remain future work.

Execution failure produces `verified_success: null`, not an invented negative
verifier result. A completed but incorrect answer produces `false`. Preserve this
distinction when defining your study's primary endpoint and failure dispositions.

## Dataset and trace layout

```text
OUTPUT/
  manifest.json                 # resolved inputs, hashes, schedule, runtime versions
  dataset.jsonl                 # one nested row per attempt, including failures
  dataset.csv                   # flat analysis/index fields, one row per attempt
  trials/trial-000001/
    attempt-0001/
      attempt.json              # durable lifecycle, pairing, provenance, outcome
      input.json                # worker input, including private enforcement config
      catalog.json              # condition's frozen catalog
      checkpoint.json           # initial messages immediately before the prompt
      events.jsonl              # execution events
      model_calls/call-000001.json  # adapter request, response, latency/error
      messages.json             # final conversation
      result.json               # worker result, when worker finishes
      evaluation.json           # checks, only for completed execution
      worker.log                # native worker/MCP diagnostic output
      runs/TRIAL-ATTEMPT/        # normal engine transcripts and tool records
```

Manifests record separate evaluator/engine source hashes, the configured CAF path
and interpreter, and engine runtime dependency versions. Executable symlink paths
are preserved so a CAF virtual environment retains its installed dependencies.

Records include experiment/task/condition/scenario/family/split IDs, `pair_id`,
repetition, trial ID, attempt number, spec/source/artifact hashes, status, elapsed
wall time, final answer when available, and verifier outcome. Pair conditions using
`pair_id`; do not treat retries as independent trials. Exports intentionally retain
all attempts. Choose and report a prespecified attempt-selection rule for analyses.
There is no statistical aggregation or train/test dataset conversion yet.

Writes of lifecycle JSON and manifests use flushed, fsynced temporary files followed
by replacement. Exports are rebuilt after each attempt and on demand. A hard crash
can leave a `running` record; resume marks it `interrupted` and creates a new attempt.
A normal resume skips terminal attempts. `--retry-failed` additionally retries
execution failures and completed attempts with a failed verifier. Earlier records
are retained. Existing output requires `--resume`; changed resolved inputs, source,
or tracked dependency versions require a new output directory.

The manifest records Python version, selected installed dependency versions, and a
hash of top-level Python sources, evaluation modules, and requirements. It is a
provenance record, not an archived environment or tamper-proof database.

## Telemetry, interaction, and lifecycle

The recording adapter captures all engine `chat` calls, including summaries and
retries. Requests are captured at the engine/provider-adapter boundary; these are
not guaranteed byte-for-byte HTTP wire payloads. Provider raw responses are retained
when exposed by the existing adapter. Available `usage` objects are included in
results; Ollama-specific counters remain in individual response records.

`usage_complete: false` and `nested_operation_telemetry_complete: false` explicitly
mark the current measurement limits. There is no normalized total token/currency
cost, and internal model calls or subprocess work inside generated tools are not
fully measured. Missing measurements must not be interpreted as zero.

Each attempt uses a fresh worker, native MCP server, messages, catalog path, and log
root. No prior conversation is resumed. Human approval, timeout decisions, and
post-tool retry decisions end the trial as `interaction_required`; unattended
execution never silently auto-approves. The wall deadline terminates the worker
process group and tracked descendants. Ctrl-C records interruption and stops the
active worker. Cleanup is best effort; detached remote processes and persistent
network effects are outside this runner's control.

The target lock coordinates cooperating evaluation runners on the same host. The
WebUI, ordinary CLI, other hosts, and external operators do not honor it. Reserve
the target operationally. ScenarioForge import validates historical readiness
evidence; it does not deploy, probe or reset the network between trials. Use a
prepared environment and validate restoration before repeating state-changing tasks.
Process isolation does not establish scenario restoration.

Evaluator definitions are absent from participant inputs, but workers and tools
currently share the host filesystem and user permissions with the coordinator.
**This is not an evaluator security boundary.** Shell-capable tools could read
experiment files. Use separate evaluator storage/permissions or a sandbox before
conducting held-out studies with tools that can access those files. Treat raw traces
as private and review/redact them before publishing a dataset. The included
no-tools example avoids giving the agent filesystem access.

## Module boundaries and next steps

* `cyber_agent_flow_eval/execution_service.py`: headless adapter around `MCPSession`, checkpoint capture,
  event recording, interaction policy, and model-call recording.
* `cyber_agent_flow_eval/spec.py`: YAML contract, resolved catalog/guidance snapshots, hashes,
  and deterministic schedule construction.
* `cyber_agent_flow_eval/runner.py`: target lease, worker lifecycle, attempts, verifiers, export.
* `cyber_agent_flow_eval/worker.py`: participant-only process entry point.
* `cyber_agent_flow_eval/scenarioforge.py`: versioned suite import, integrity and readiness gates.
* `cyber_agent_flow_eval/storage.py`: atomic JSON persistence.

The existing CLI/WebUI still instantiate the same engine directly; migrating their
lifecycle management onto a common service is a later refactor. Engine additions
are optional tool allowlisting, explicit guidance, and a process-specific run root,
with existing defaults preserved.

Next research milestones are executable snapshots, evaluator sandboxing, supported
model generation controls across all calls, normalized usage and nested-operation
accounting, ScenarioForge readiness/restoration, independent environment verifiers,
and paired statistical analysis. ScenarioForge package import is available below; automated deployment/restoration remains future work. The research plan remains the specification for
those larger study requirements.

## Validation

```bash
.venv/bin/python -m pytest -q
```

Tests include a real native MCP worker with a local fake model endpoint, verifying
condition-specific guidance, zero-tool selection, checkpoint/response capture, and
no shared catalog changes. Other tests cover validation, repeat schedules, locks,
resume, changed inputs, retries, interrupted records, and blocked tool dispatch.
No live model or external target is needed for these tests.

## Example: Kali attached to a ScenarioForge network

Use [scenarioforge-observational.yaml](../configs/experiments/scenarioforge-observational.yaml)
for a small live service-inventory comparison. It has a minimal nmap catalog and
an accompanying guidance document. Both conditions expose the same nmap tool;
only one receives the extra guide.

Assume you have already deployed the scenario and connected Kali to its network.
The example addresses are placeholders: `10.77.0.10` with TCP 22/80 open and
`10.77.0.20` with TCP 443 open. Replace the addresses in both the prompt and allow
list, replace the verifier's expected values using independently confirmed lab
truth, and set `scenario_id` to your frozen scenario identifier. The runner treats
that identifier as metadata and does not look it up in ScenarioForge.

On Kali, with CAF installed and its paths selected in the evaluator YAML:

```bash
command -v nmap
ip route get 10.77.0.10
ip route get 10.77.0.20

# Optional operator preflight against your example lab; not an evaluation trial.
nmap -sT -n -Pn -p 22,80,443 --host-timeout 45s 10.77.0.10 10.77.0.20
```

The route must lead to the ScenarioForge lab network. A failed scan may mean
routing, firewalling, or an unready service rather than an agent failure. Confirm
expected services from the scenario's evaluator/facilitator information and actual
readiness; do not blindly turn an unexpected preflight result into ground truth.
These checks do not automatically populate the experiment's readiness records.

Set `model.name` to your installed tool-capable Ollama model. If Ollama runs on a
separate inference machine, set `model.url` to its reachable URL instead; tools
still execute on Kali.

```bash
.venv/bin/cyber-agent-flow-eval plan configs/experiments/scenarioforge-observational.yaml
.venv/bin/cyber-agent-flow-eval run configs/experiments/scenarioforge-observational.yaml \
  --output eval-runs/scenarioforge-inventory-001
```

This runs six sequential trials: one task × two conditions × three repetitions.
Each worker starts from fresh conversation state; the deployed network is reused.
Keep the lab reserved and unchanged while comparing conditions. A fresh worker
is not a reset of the network. The order seed randomizes condition order only.

Inspect `dataset.csv` for status and verified success, `dataset.jsonl` for full
attempt rows, and each attempt's `evaluation.json`, model calls, and engine tool
records for evidence. `json_equals` checks the final inventory; it does not enforce
that the answer was derived from valid scan evidence, so review traces for this
pilot. Six trials are a workflow demonstration, not a statistically sufficient study.

To resume an interrupted run, repeat the command with `--resume`. If you change
addresses, expected values, guidance, or model configuration, use a new output
directory. The evaluator still shares host permissions with the agent's tools;
this development example does not establish held-out ground-truth isolation.

## Import a ScenarioForge evaluation package

ScenarioForge can now prepare the package after a successful WebUI execution; download it from **Reports → Download → Evaluation package (ZIP)**. CLI users can add `--evaluation-export` to `execute`. Unzip into a private directory before importing. CAF owns execution scope; import preserves the template allow/disallow lists. Readiness failures still block evaluation.

ScenarioForge now supports `evaluation-export`. Its version 3 package contains
participant task definitions, separate evaluator verifiers and
required checks, saved XML/attack graph snapshots, file hashes, and deployment
readiness evidence. The export adds no hints and does not change artifact conditions.

On the ScenarioForge machine, after deploying the saved XML, run:

```bash
python -m scenarioforge.cli check-artifacts \
  --xml /path/to/deployed.xml --scenario Training \
  --host core-vm.example --session-id 9 --strict > /path/to/readiness.log

# Proceed only when checks succeed; all example paths/addresses need replacement.
python -m scenarioforge.cli evaluation-export \
  --xml /path/to/deployed.xml --scenario Training \
  --suite-id training-dev-v1 --session-id 9 \
  --readiness-report /path/to/readiness.log \
  --output-dir /path/to/training-dev-v1
```

The default task asks for flags from nodes with resolved flag values, with a private
`flags_match` verifier. Default required readiness checks are containers, services,
ports and injects. ScenarioForge also accepts `--evaluation-tasks reviewed-tasks.json`
for authored inventory/reachability tasks with `json_equals` or `contains_all`, or
selected `flag_nodes`. Use an inventory task for the existing nmap-only template;
flag challenges generally need a different tool set. ScenarioForge's
`docs/EVALUATION_EXPORT.md` documents authoring and includes an inventory example.

Transfer the package to a private location on the evaluation coordinator. On Kali:

```bash
.venv/bin/cyber-agent-flow-eval import-suite /path/to/training-dev-v1 \
  --config configs/experiments/scenarioforge-observational.yaml \
  --output configs/experiments/training-dev-v1.yaml

.venv/bin/cyber-agent-flow-eval plan configs/experiments/training-dev-v1.yaml
.venv/bin/cyber-agent-flow-eval run configs/experiments/training-dev-v1.yaml \
  --output eval-runs/training-dev-v1
```

`import-suite` preserves model, conditions, budgets and repetitions from the runtime
configuration, rebases file paths, replaces tasks with a suite reference, and preserves
the template execution network policy, including all exclusions. It refuses to overwrite an existing output YAML. Review
model and tools before running. Conditions still control the experimental change:
all receive the same participant tasks and scope.

Alternatively, replace inline `tasks` in your YAML with:

```yaml
suite:
  path: /path/to/training-dev-v1
  max_readiness_age_seconds: 3600
```

See [the suite configuration example](../configs/experiments/scenarioforge-suite.example.yaml).
Only one of `tasks` and `suite` is permitted. Known objective IPs must be permitted by CAF’s execution network policy. Conflicts
block import/plan/run without modifying that policy or dropping objectives. Custom
tasks without source-node references have no automatic target compatibility check.
Version 1 packages remain readable, but their policy does not override CAF; re-export
to remove legacy scope text from prompts. Ordinary flag prompts name target IPs; authored discovery tasks omit them.

`plan` reports readiness eligibility without executing anything. Draft packages
without readiness can be imported and planned, but `run` refuses them. Before the
run and before each new attempt, the runner checks package hashes, matching XML and
CORE host/session identity, confirmed-session status, passing task-required checks,
and report age. Required checks cannot be skipped; other checks may be skipped but
must not fail or warn. Reports with future timestamps beyond clock-skew tolerance
are rejected. Default maximum age is one hour; choose a study-appropriate limit at
import with `--max-readiness-age-seconds` or in YAML before freezing the run.

These are **historical readiness records**, not live per-trial probes or restoration.
When evidence expires, collect new checks, export a new package, and use a new
configuration/output run. Different evidence has a different package hash. Keep
state-changing tasks out of controlled repetitions until you have validated reset
coverage. Source XML and graph values do not independently prove deployed truth.

For `flags_match`, the final answer must be:

```json
{"flags": {"objective-id": "recovered flag"}}
```

Each objective is checked by exact string match; missing flags fail. `score` is the
fraction of objectives matched, while `verified_success` requires all matches,
valid format, and no unknown objective IDs. Graph attack order is not required.
Evaluation records contain objective IDs and booleans, not expected flag strings.
The full expected values remain in the private suite and frozen experiment manifest.

Attempts and CSV/JSONL now record suite ID, package hash, CORE session ID and
readiness timestamp. JSONL also carries the scenario snapshot identity and readiness
file hash. Per-objective checks and optional partial score are saved alongside the
usual answer, checkpoint, tool records, and model calls.

The importer validates file integrity and audience structure. **It does not provide
OS isolation:** evaluator files and experiment output must be inaccessible to
participant tools through separate users/permissions or sandboxing for held-out
studies. Same-user filesystem access can expose answers despite private file modes.
Flags returned successfully will also appear in raw traces; redact before publishing.

In air-gapped labs, manually transfer the evaluation ZIP from app-vm to participant-vm.
CAF requires no live connection to ScenarioForge; target traffic uses HITL.
