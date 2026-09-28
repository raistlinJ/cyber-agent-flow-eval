# Architecture and evaluation PNG provenance

Updated September 28, 2026 using the built-in image generation tool. The previous
architecture PNG was the edit target; the evaluation flow is a new companion image.
The diagrams separate the host orchestrator and host evaluator from the participant
worker. Matching PNGs are stored in the CAF, evaluator and orchestrator repositories.

- `scenarioforge_cyber-agent-flow.png`: topology and workflow sequence.
- `scenarioforge_cyber-agent-flow-eval.png`: study inputs, conditions, trials,
  private scoring and human review.

Commands and limitations also appear in the orchestrator's
`docs/evaluation-diagrams.md`. The figures were visually reviewed against current
code and documentation; generation is not evidence of a live deployment test.

## Architecture prompt

```text
Edit the provided architecture infographic into an updated, polished, accurate technical diagram. Use the supplied image as edit target and visual reference: retain white background, dark navy typography, pale purple host, blue ScenarioForge, teal CORE, amber participant, simple vector-like icons, crisp arrows. Redesign spacing as needed. Large landscape 3:2 image, about 3000x2000, sharp readable text. No decorative 3D or gradients that impair text.

Title: "ScenarioForge + CyberAgentFlow"
Subtitle: "Host orchestration • isolated lab execution • repeatable evaluation"

Top wide container "Proxmox host", with two distinct software cards and an arrow from the first to the second:
"cyber-agent-flow-orchestrator"
"WebUI / CLI • workflow YAML • VM roles"
"Deploy or reuse • prepare artifacts • monitor results"
second card "cyber-agent-flow-eval"
"Trial scheduling • private scoring • datasets"
"Separate evaluator library / CLI, running on the host"
A small line in the host: "PVE login + VM permissions • per-user run storage"

Below, three horizontally arranged VM cards:
LEFT blue: "app-vm" / "ScenarioForge"
"Execute a saved scenario or reuse an export"
"Export tasks, starting facts and private verifiers"
CENTER teal: "corevm" / "CORE scenario network"
"Subnets • services • challenges • flags"
"Discover hidden networks through scenario clues"
RIGHT amber: "participant-vm / Kali" / "CyberAgentFlow"
"Shared engine + controlled tools + selected artifacts"
"Thin evaluation worker per trial"
"CAF enforces the configured network scope"

Dashed purple bidirectional host-to-app arrow labeled "QEMU Guest Agent: commands + export".
Dashed purple bidirectional host-to-participant arrow labeled "QEMU Guest Agent: inputs + commands + results".
A small dashed host-to-core line labeled "VM / guest status"; do not obscure labels.
Solid blue arrow app to core labeled "Deploy / manage".
Solid teal bidirectional arrow core to participant labeled "HITL: lab traffic".
Do NOT draw any direct app-to-participant connection.

A distinct boundary caption:
"No direct app-vm ↔ participant-vm network link"
"Host-mediated transfers need no guest IP or SSH. Participant still needs lab and model connectivity."
"Private answers and full attack graphs stay in app-vm / host storage."

Bottom sequence: three numbered horizontally arranged cards:
"1  Configure + review"
"Choose VM roles, saved scenario/export,"
"model, scope, conditions and budgets."
code "$O plan workflow.yaml"
"2  Run the workflow"
"Export → optional artifact prep → trials"
code "$O run workflow.yaml --output runs/study"
"3  Inspect + export"
"Scores, progress, traces and datasets"
code "$O results runs/study"
code "$O export runs/study --destination exports/study"

Caption below commands, exact:
"Host CLI: O=.venv/bin/cyber-agent-flow-orchestrator"
"workflow.yaml represents an edited workflow from examples/; use PVE-authenticated user-run for account-scoped CLI runs."
Footnote: "Reset/readiness steps require configured hooks. Other desktop hypervisor backends remain placeholders."
Do not include old eval fetch/import commands, fixed VM IDs, automatic hints, automatic resets, or an evaluator coordinator inside participant-vm. Text must be legible and correctly spelled.
```

## Evaluation flow prompt

```text
Create a polished technical infographic PNG, landscape 3:2 roughly 3000x2000, with white background, navy typography, pastel purple host, blue inputs, amber guest execution, teal output. Crisp vector-like cards, flat icons, precise arrows, generous whitespace, readable type. It complements an architecture diagram for ScenarioForge and CyberAgentFlow. No unnecessary decoration.

Title: "How orchestrated evaluation works"
Subtitle: "Do generated artifacts help the agent get further, faster?"

Use a clear numbered flow, top row 1→2→3 on HOST, center loop showing guest execution and private host scoring, bottom output. Explicitly label who owns each step.

1 card "Configure the study" / "Human / workflow YAML"
"Saved scenario or existing evaluation ZIP"
"Model + scope + turn/time budgets"
"Conditions + repetitions + order seed"
"Prompts from task YAML or imported suite"

2 card "Prepare inputs" / "Orchestrator on host"
"Deploy/export or fetch existing package"
"Check readiness; retain private verifiers"
"Optional configured generation/test commands"
"Select fixed artifacts before evaluation"
Small blue source pill pointing into this card: "ScenarioForge on app-vm"

3 card "Build the trial schedule" / "Evaluator on host"
"Tasks × conditions × repetitions"
"Example: 2 tasks × 2 conditions × 3 repeats"
"= 12 trials"
"Record configuration and artifact hashes"

Middle large area labeled "Repeat for each trial" with two distinct side-by-side regions:
LEFT "Participant VM — thin worker + CAF engine"
Small arrow entering from schedule labeled "QEMU Guest Agent: participant inputs only"
"One task prompt + starting facts"
"Selected tool catalog + optional guidance"
"Model ↔ tool calls within turn/time limits"
"Lab actions over HITL → CORE challenges"
"Return response, tool traces and timings"
RIGHT "Host evaluator — private scoring"
"Compare final answer with private verifiers"
"Record flags observed and partial progress"
"Measure time to first flag and time-budget progress"
"Record success, timeout or error"
Connect guest LEFT to private host RIGHT with arrow "Collect results through guest agent".
A loop arrow from scoring back to trial input labeled "Next scheduled trial".
Small text above the loop: "Configured reset/readiness hooks run before each attempt; reset is not automatic."
Small lock callout in host scoring: "Expected answers and full attack graphs are never staged to the participant."

Separate visible comparison strip:
"Baseline: controlled tool set"
"Added artifacts: same tool set + selected generated tool and/or guidance"
"Keep task, starting facts, model and budgets matched; restore comparable lab state."

Bottom two connected cards:
"Review and export" / "Orchestrator WebUI / CLI"
"Condition summaries • trial status • logs"
"Evaluator: JSONL / CSV datasets and provenance"
"Human decision"
"Inspect whether added artifacts improve progress or speed"
"Revise/test artifacts between studies, then rerun"
Small human icon; emphasize teaming, no fully autonomous generation loop.

Footer: "Artifact generation and repair stay outside scored trials. Catalog/guidance snapshots do not freeze executable dependencies."
Footer command: "$O results runs/study"
Footer alias: "O=.venv/bin/cyber-agent-flow-orchestrator"
Important: distinguish separate orchestrator from evaluator; both coordinators on host. Do not depict evaluator coordinator in VM. No automatic hints, no promise of auto scenario reset, no network link app-vm to participant. No security scans of public networks. This is a controlled scenario lab.
```
