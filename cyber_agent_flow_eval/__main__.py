import argparse
import json
import sys
import yaml

from .runner import CleanupError, export, lease, run
from .spec import digest, resolve, schedule


def main(argv=None):
    parser = argparse.ArgumentParser(prog='cyber-agent-flow-eval', description='Separate evaluator using a configured CyberAgentFlow checkout')
    commands = parser.add_subparsers(dest='command', required=True)
    ingest = commands.add_parser('import-suite', help='Create YAML from a ScenarioForge package and a runtime template')
    ingest.add_argument('package')
    ingest.add_argument('--config', required=True, help='Existing YAML supplying model, conditions, and budgets')
    ingest.add_argument('--output', required=True)
    ingest.add_argument('--max-readiness-age-seconds', type=int, default=3600)
    plan = commands.add_parser('plan', help='Validate and print the schedule without executing')
    plan.add_argument('spec')
    execute = commands.add_parser('run', help='Run isolated sequential trials')
    execute.add_argument('spec')
    execute.add_argument('--output', required=True)
    execute.add_argument('--resume', action='store_true')
    execute.add_argument('--retry-failed', action='store_true')
    dump = commands.add_parser('export', help='Rebuild JSONL and CSV from attempt records')
    dump.add_argument('output')
    args = parser.parse_args(argv)
    try:
        if args.command == 'import-suite':
            from .scenarioforge import import_config
            print(json.dumps(import_config(args.package, args.config, args.output, args.max_readiness_age_seconds), indent=2))
        elif args.command == 'plan':
            spec = resolve(args.spec)
            result = {'engine': spec['engine'], 'spec_hash': digest(spec), 'schedule': schedule(spec)}
            if 'suite_snapshot' in spec:
                from .scenarioforge import require_ready
                snapshot = spec['suite_snapshot']
                result['suite'] = {'id': snapshot['id'], 'package_hash': snapshot['package_hash'], 'scenario': snapshot['scenario']}
                try:
                    require_ready(snapshot, spec['suite']['max_readiness_age_seconds'])
                    result['readiness'] = {'ready': True}
                except ValueError as exc:
                    result['readiness'] = {'ready': False, 'reason': str(exc)}
            print(json.dumps(result, indent=2))
        elif args.command == 'run':
            if args.retry_failed and not args.resume:
                parser.error('--retry-failed requires --resume')
            rows = run(args.spec, args.output, resume=args.resume, retry_failed=args.retry_failed)
            latest = {r['trial_id']: r for r in rows}
            return 0 if all(r['status'] == 'completed' for r in latest.values()) else 1
        else:
            from pathlib import Path
            if not (Path(args.output) / 'manifest.json').is_file():
                raise ValueError('No experiment manifest in output directory')
            with lease(Path(args.output) / '.coordinator.lock'):
                print(f'Exported {len(export(args.output))} attempts')
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError, CleanupError) as exc:
        print(f'Evaluation error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
