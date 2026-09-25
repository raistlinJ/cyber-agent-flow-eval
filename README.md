# cyber-agent-flow-eval

A separate evaluation application for repeatable comparisons of generated tools
and guidance. It imports ScenarioForge task packages, runs YAML-defined trials,
scores answers, and writes JSONL/CSV datasets.

The main **cyber-agent-flow** project owns interactive sessions, analysis, artifact
generation, repair and artifact tests. This project owns experimentation. It loads
the shared CAF engine and native MCP tools from a checkout you select; it does not
vendor them, start the WebUI, or import Flask into its coordinator.

## Install

Use Python 3.10+ on Linux/macOS. Install this project in its own environment:

```bash
cd cyber-agent-flow-eval
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Install/configure CAF separately, including the Python dependencies and Kali tools
needed by your experiment. The evaluator environment needs only PyYAML and psutil;
workers can use CAF's own Python environment. No model, Docker image, or target is
started during installation or planning.

## Select the shared engine

Every evaluation YAML requires an `engine` section:

```yaml
version: 1
id: my-study
engine:
  path: /opt/cyber-agent-flow
  python: /opt/cyber-agent-flow/venv/bin/python
# model, execution, tasks/suite, conditions, repetitions follow
```

`engine.path` selects the checkout containing `mcp_client.py`, `mcp_kali.py`, and
`session_logger.py`. `engine.python` is an optional executable path; if omitted,
workers use the evaluator's Python interpreter, which must then have CAF's
dependencies installed. Paths can be absolute or relative to the YAML file, and
`~` is supported. An interpreter path is a filename, not a shell command or PATH
lookup. Virtual-environment symlinks are preserved.

The selected CAF revision must support tool allowlisting, explicit guidance and
hidden network policy in `MCPSession`. Planning validates these controls without
importing CAF. Workers run in fresh processes, load CAF from `engine.path`, use that
directory as their working directory, and start its native MCP server with the
chosen interpreter. Catalog and log paths remain specific to the trial.

## Run

The examples assume sibling checkouts and CAF's environment at `venv/bin/python`.
Edit `engine`, model, scope and artifact paths to match your installation:

```bash
.venv/bin/cyber-agent-flow-eval plan configs/experiments/example.yaml
.venv/bin/cyber-agent-flow-eval run configs/experiments/example.yaml \
  --output eval-runs/example
```

Equivalent module invocation: `.venv/bin/python -m cyber_agent_flow_eval ...`.
The installed CLI works from any directory; YAML paths resolve relative to the
YAML file, while `--output` resolves relative to your current directory.

For ScenarioForge, manually transfer the evaluation ZIP in the air-gapped lab:

```bash
unzip suite.zip -d eval-inputs/suite
.venv/bin/cyber-agent-flow-eval import-suite eval-inputs/suite \
  --config configs/experiments/scenarioforge-suite.example.yaml \
  --output configs/experiments/study.yaml
.venv/bin/cyber-agent-flow-eval plan configs/experiments/study.yaml
.venv/bin/cyber-agent-flow-eval run configs/experiments/study.yaml \
  --output eval-runs/study
```

The supplied nmap-only conditions are for inventory comparisons, not general flag
challenges. Configure suitable tools and model before importing. Import preserves
the configured engine, interpreter, scope, conditions and budgets, rebasing paths
when the output YAML is elsewhere. It does not edit CAF's interactive configuration.

## Records and repeatability

Each task × condition × repetition is a trial with a fresh conversation. Attempts,
checkpoints, model calls, tool records and verifier outputs belong to this project’s
chosen output directory. Manifests record separate evaluator and CAF source hashes,
the configured engine/interpreter, and both runtime identities. Resume refuses changed
configuration, recorded source or dependency identities. External tool binaries,
model weights and live scenario state still require separate versioning/restoration.

Generation/testing remain in the main app. This evaluator consumes selected artifacts;
it does not repair tools, reset CORE, provide automatic hints or isolate private
files from same-user tools. Discovery scope stays enforced while omitted from model
prompts. See [the evaluation contract](docs/experiments.md) and
[the generation-to-evaluation workflow](docs/artifact-evaluation-workflow.md).

## Tests

```bash
.venv/bin/python -m pytest -q
```

Tests default to a sibling `cyber-agent-flow` checkout and its `venv/bin/python`.
Override `CAF_TEST_ENGINE_DIR` and `CAF_TEST_ENGINE_PYTHON` for another installation.
Integration tests use a local mock model and include a relocated CAF checkout with
spaces in its path. They do not contact live models or CORE targets.

## Migration from the embedded evaluator

The former CAF `experiments/` package, execution adapter, evaluation configs and tests
have moved here. Replace `python -m experiments` with `cyber-agent-flow-eval` or
`python -m cyber_agent_flow_eval`, add `engine.path` (and normally `engine.python`),
and rebase any relative catalog/guidance paths when moving your YAML. ScenarioForge
package versions 1–3 remain readable. Start a new output directory: old run manifests
cannot resume across this source/configuration change. Old datasets remain inspectable
and can be re-exported with the `export` command.
