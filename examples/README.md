# Three small experiments

These examples run the evaluator **on the Proxmox host** and CAF **inside
participant-vm**. Run the commands below from the `cyber-agent-flow-eval` checkout
on the node owning the VMs. They use the default ScenarioForge VM IDs: corevm
9401, app-vm 9402, participant-vm 9403.

![Host evaluation architecture](../scenarioforge_cyber-agent-flow.png)

| Example | Comparison | Trials | Target needed |
| --- | --- | --- | --- |
| [01-smoke.yaml](01-smoke.yaml) | One supplied observation, no tools | 1 | None |
| [02-tools-vs-added.yaml](02-tools-vs-added.yaml) | Three controlled tools vs the same three plus one helper | 6 | Included read-only HTTP fixture |
| [03-scenarioforge-runtime.yaml](03-scenarioforge-runtime.yaml) | The same comparison on exported challenges | 4 per imported task | Running ScenarioForge scenario |

## Common preparation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
E=.venv/bin/cyber-agent-flow-eval
```

In **each YAML you use**, set `model.provider`, `model.url`, and `model.name` for
your deployment. The supplied `localhost:11434` means **participant-vm's** localhost,
not the Proxmox host. Adjust guest IDs, the participant account, and guest CAF paths
if you changed the provisioner defaults. CAF and the tool dependencies must already
be installed in participant-vm. Authenticated models can use a guest
`backend.environment_file` and `model.api_key_env`; see [the Proxmox guide](../docs/proxmox.md).
Use the updated CAF engine that matches numeric URL hosts against IP/CIDR policy
entries. Older revisions can reject an HTTP URL even when its IP is allowed;
update the engine rather than widening scope to `*`.

No private expected answers are staged into participant-vm. These demo flags are
intentionally published examples, however, so they are not held-out research data.

## 1. Check the complete execution path without tools

```bash
"$E" check-backend --config examples/01-smoke.yaml
"$E" plan examples/01-smoke.yaml
"$E" run examples/01-smoke.yaml --output eval-runs/01-smoke
```

The model receives one supplied observation and should return
`{"open_ports":[80]}`. There is one fresh conversation, up to three engine turns,
and a 120-second wall budget. This checks guest staging, model access, execution,
collection, and exact-answer scoring. It measures neither discovery nor tool quality.

## 2. Compare controlled tools with an added helper

This stateless fixture serves four in-memory HTTP pages with two demo flags.
It runs on **corevm's HITL address**, outside the CORE-emulated scenario; its
purpose is a small experiment you can run before importing a full scenario.
It does not serve the corevm filesystem or change an existing scenario.
The fixture installs its source under `/var/lib/caf-eval-http-demo/`.

Start the fixture from the Proxmox host through the guest agent:

```bash
qm guest exec 9401 --pass-stdin 1 -- /usr/bin/python3 - \
  --bind 10.254.200.3 --port 8080 < examples/http-demo.py
qm guest exec 9401 -- systemctl is-active caf-eval-http-demo.service
qm guest exec 9403 -- curl --fail --silent --show-error --max-time 5 \
  http://10.254.200.3:8080/
```

Check the returned guest **`exitcode` is 0**, the service is `active`, and the last
command returns the demo index. A successful `qm` invocation alone does not prove
the guest command succeeded. If it returns a PID without an exit status, poll it
with `qm guest exec-status VMID PID`. Use an unused port; if your core HITL address
or port differs, change the installer arguments, task URL, and YAML scope together.
The fixture uses a dedicated systemd service, replaces that service when rerun, and
automatically stops after one hour. It uses an unprivileged dynamic account.

Now run the comparison:

```bash
"$E" plan examples/02-tools-vs-added.yaml
"$E" run examples/02-tools-vs-added.yaml --output eval-runs/02-tools-vs-added
```

Both conditions receive the same prompt: begin at the demo URL, follow its links,
and recover two flags. The prompt contains no answers.

- **Baseline:** `nmap`, `curl`, `python3`.
- **Added helper:** those exact same definitions plus `http_flag_walk`.

The [helper source](artifacts/http_flag_walk.py) is an **illustrative, hand-authored
stand-in** for a generated artifact. It follows up to six same-origin links with
bounded HTTP requests and reports flag strings. It knows the flag format, not
the expected values or page locations. Its source is embedded in the treatment
catalog, so no helper script needs a separate guest transfer. The helper is not a
general challenge solver; its purpose is to demonstrate adding a specialized tool
while holding the baseline constant.

There are **six trials**: one task × two conditions × three repetitions. Each trial
starts a fresh conversation with up to 12 engine turns and 120 seconds. Condition
order is reproducibly shuffled within each repetition. The target is read-only,
so these demo trials do not need a reset hook.

The maximum configured worker time is 12 minutes; staging, collection, and other
orchestration add time. Model-call counts are not identical to turn limits because
summaries and retries can make additional calls. There is no guaranteed treatment
improvement: both conditions may solve this small task successfully.

Inspect the resulting table:

```bash
.venv/bin/python - <<'PY'
import csv
from pathlib import Path
path = Path('eval-runs/02-tools-vs-added/dataset.csv')
for r in csv.DictReader(path.open()):
    print(r['pair_id'], r['condition_id'], r['status'],
          'final_score=' + r['score'],
          'observed_flags=' + r['flags_observed'],
          'first_flag_seconds=' + r['time_to_first_flag_seconds'],
          'execution_seconds=' + r['execution_seconds'])
PY
```

Compare paired repetitions by `pair_id`. `score` measures final-answer flag recovery;
`progress.json` records when expected flags first appeared and counts at 15, 30,
60, and 120 seconds. A timed-out run can retain observed progress without a final
score. These are flag observations, not independent proof of an exploit or route.
Three repetitions make this a workflow demonstration, not a statistical conclusion.

Stop the fixture when finished:

```bash
qm guest exec 9401 --pass-stdin 1 -- /usr/bin/python3 - --stop < examples/http-demo.py
```

To edit the example helper, regenerate its frozen catalog **before** starting a
new experiment:

```bash
.venv/bin/python examples/build_catalogs.py
```

For your actual study, substitute CAF-generated artifacts that have completed
your generation/testing workflow. Preserve baseline definitions and guidance, and
change only the artifact additions you intend to measure. Executable scripts and
dependencies referenced by your own catalogs must exist in participant-vm; unlike
this inline helper, arbitrary referenced executables are not automatically copied.

## 3. Use your ScenarioForge challenges

Generate a fresh evaluation export through ScenarioForge's WebUI execution options
or CLI. The export must include passing readiness for the deployed scenario.
Then edit [03-scenarioforge-runtime.yaml](03-scenarioforge-runtime.yaml) for the real
model, authorized subnets/exclusions, controlled tools, generated artifacts, and
budgets. Its example HTTP helper is only useful for HTTP flag tasks; select the
tools your actual challenges require.

The runtime file is an **import template**, not a standalone experiment: imported
tasks, prompts, starting facts, and private verifiers come from the package.

```bash
"$E" fetch-suite --config examples/03-scenarioforge-runtime.yaml \
  --guest-path /absolute/path/in/app-vm/evaluation-export.zip \
  --output eval-inputs/example-suite

"$E" import-suite eval-inputs/example-suite \
  --config examples/03-scenarioforge-runtime.yaml \
  --output eval-inputs/example-study.yaml

"$E" plan eval-inputs/example-study.yaml
"$E" run eval-inputs/example-study.yaml --output eval-runs/03-scenarioforge
```

Replace the ZIP path with the path reported by ScenarioForge. Import rebases host
catalog paths and preserves the absolute participant guest engine paths. With two
conditions and two repetitions, the schedule has **four trials per imported task**,
each with up to 20 engine turns and 300 seconds. For example, three imported tasks
produce 12 trials. Tasks can span discovery and multiple chained challenges.

Unlike the small HTTP fixture, real challenges can change network state. Configure
tested reset/readiness commands through `backend.before_trial` when needed; see
[reset hooks](../docs/proxmox.md#resets-and-readiness). A new conversation does not
restore the target. Keep starting facts, scenario state, model and budgets constant.

## Repeat, resume, or change configuration

Use a new output directory for a new experiment or changed YAML/artifacts. To
continue an unchanged experiment, use `run ... --resume`; add `--retry-failed` to
retry failed attempts. Earlier attempts remain in the dataset and should not be
counted as additional independent repetitions. Use `recover` if guest cleanup is
needed while normal resume is blocked. See [results and recovery](../docs/proxmox.md#results-timing-and-recovery).

All host instructions above use Proxmox. The PNG's `$E` shorthand means the same
`E=.venv/bin/cyber-agent-flow-eval` assignment used here. Each numbered command card
is a single invocation; join its displayed lines when typing it into a shell.
