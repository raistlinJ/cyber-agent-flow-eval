"""Rebuild the frozen sample catalogs after editing the illustrative helper."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
baseline = [
    {'name': 'nmap', 'command': 'nmap', 'allow_args': True,
     'description': 'Inspect in-scope hosts and services. Use bounded scans and explicit targets.'},
    {'name': 'curl', 'command': 'curl', 'allow_args': True,
     'description': 'Retrieve content from an in-scope URL. Use explicit URLs and bounded request times.'},
    {'name': 'python3', 'command': 'python3', 'allow_args': True,
     'description': 'Run Python 3 code for parsing or in-scope lab operations. Use explicit targets and bounded operations.'},
]
helper = {'name': 'http_flag_walk', 'command': 'python3', 'allow_args': True,
          'description': 'Fetch up to six same-origin linked pages from an in-scope HTTP(S) URL and report FLAG{...} strings. '
                         'Pass exactly one URL. Requests have five-second timeouts; redirects are refused.',
          'base_args': ['-c', (ROOT / 'artifacts/http_flag_walk.py').read_text()]}
destination = ROOT / 'catalogs'
destination.mkdir(exist_ok=True)
for name, tools in [('baseline.json', baseline), ('with-http-helper.json', baseline + [helper])]:
    (destination / name).write_text(json.dumps({'tools': tools}, indent=2) + '\n')
