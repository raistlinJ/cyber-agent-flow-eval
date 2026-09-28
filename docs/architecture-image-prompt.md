# Architecture PNG provenance

Generated with the built-in `image_gen` tool, using the previous
`cyber-agent-flow/scenarioforge_cyber-agent-flow.png` as the edit target. The
final result is saved as `scenarioforge_cyber-agent-flow.png` at the root of
both the CAF and evaluator projects. The evaluator copy is embedded in its README
and example guide. Command details and runnable examples are maintained in Markdown.

## Architecture edit prompt

```text
Use case: infographic-diagram. Edit target: the supplied architecture PNG. Replace its obsolete topology and commands with the current design below. Preserve the clean professional navy typography, white background, pale blue/teal/amber cards, simple flat icons, and crisp arrows. Recompose as a spacious large landscape technical infographic, ideally 2400x1600 or larger. Exact spelling is essential. Do not keep the manual ZIP transfer banner, old python -m experiments commands, or evaluator coordinator inside participant-vm.

Title: "ScenarioForge + CyberAgentFlow evaluation"
Subtitle: "Do added artifacts help the agent get further, faster?"

Topology upper two thirds:
Top wide purple-accent card: "Proxmox host" and "cyber-agent-flow-eval — separate coordinator". Contents: "YAML • schedule • private scoring • JSONL / CSV datasets", "Fetch export → stage trial → collect results", "Final flag score + timestamped flag progress".
Below it three clear cards horizontally:
Left blue "app-vm · 9402", "ScenarioForge", "Generate / deploy / check readiness", "Export tasks, starting facts, private answers".
Center teal "corevm · 9401", "CORE target networks", "Entry subnet → clues → hidden subnet", "Discover and solve challenges / recover flags".
Right amber "participant-vm · 9403", "CyberAgentFlow shared engine + tools", "Fresh worker per trial", "Baseline tools → same tools + generated tools", "Guest timeout + cleanup", "CAF enforces allow / disallow".
Dashed purple bidirectional connection ONLY between host and app labeled "QEMU Guest Agent: export transfer". Dashed purple bidirectional connection ONLY between host and participant labeled "QEMU Guest Agent: files + commands + results". These denote host/guest channels, not network paths.
Solid blue app→core arrow: "Deploy / management".
Solid teal participant↔core arrow: "Agent traffic over HITL".
No direct app↔participant line or connection.
Standalone clear caption: "No app-vm ↔ participant-vm network link. Guest-agent control needs no guest IP or SSH."
Private ground truth stays in app export and host coordinator; never draw private answers transferred to participant. Guest still needs scenario/model network access.

Lower third: readable numbered workflow cards, exact compact commands, avoid code wrapping confusion:
1. "app-vm: deploy + export"
"python -m scenarioforge.cli execute"
"--xml scenario.xml --scenario Training"
"--evaluation-export"
2. "host: fetch package"
"eval fetch-suite --config runtime.yaml"
"--guest-path /path/to/suite.zip"
"--output eval-inputs/suite"
3. "host: import study"
"eval import-suite eval-inputs/suite"
"--config runtime.yaml --output study.yaml"
4. "host: run comparison"
"eval run study.yaml --output eval-runs/study"
Caption below commands: "eval = .venv/bin/cyber-agent-flow-eval; command lines within each card are one invocation. Replace sample paths."

Bottom compact strip: "Same starting facts + scenario state + model + budgets" | "More flags • time to first flag • progress at time limits"
Footnote: "Optional reset/readiness hooks run before each attempt. Expected flags remain private. macOS / Linux / Windows guest backends: placeholders."
Do not suggest automatic hints, built-in scenario reset, guest networking via host, or completed live testing. All text must be readable and diagrams unambiguous.
```

## Final correction prompt

```text
Edit this infographic with exactly one typography correction. Preserve ALL topology, layout, artwork, colors and other text. In workflow cards 2, 3, and 4 replace the command-prefix word `eval` with literal `$E` (dollar sign then uppercase E). Thus commands start `$E fetch-suite`, `$E import-suite`, and `$E run`. In the caption immediately below those cards replace `eval = .venv/bin/cyber-agent-flow-eval` with exactly `E=.venv/bin/cyber-agent-flow-eval`. Keep the remainder of caption: `command lines within each card are one invocation. Replace sample paths.` This makes the diagram use a valid shell variable instead of the shell's built-in eval command. Make no other changes.
```
