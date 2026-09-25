"""Private worker: its input contains participant data, never verifier answers."""
import asyncio
import sys
from pathlib import Path

import os

# Direct script launch works with a different Python environment than the coordinator.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_eval.storage import read_json, write_json
from cyber_agent_flow_eval.execution_service import execute

if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    config = read_json(directory / 'input.json')
    os.chdir(config['engine']['path'])
    result = asyncio.run(execute(config, directory))
    write_json(directory / 'result.json', result)
