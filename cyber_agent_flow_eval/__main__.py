import argparse
import json
import sys
import yaml
import zipfile

from .runner import CleanupError, PreparationError, export, lease, run
from .spec import digest, resolve, schedule


def main(argv=None):
    parser = argparse.ArgumentParser(prog='cyber-agent-flow-eval', description='Separate evaluator using a configured CyberAgentFlow checkout')
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch-suite', help='Fetch a ScenarioForge ZIP from app-vm through the Proxmox guest agent')
    fetch.add_argument('--config', required=True)
    fetch.add_argument('--guest-path', required=True, help='Absolute ZIP path inside app-vm')
    fetch.add_argument('--output', required=True, help='New host directory for the validated suite')
    check = commands.add_parser('check-backend', help='Check configured guest engine/runtime before running trials')
    check.add_argument('--config', required=True)
    recover = commands.add_parser('recover', help='Stop and collect journaled Proxmox attempts without restarting trials')
    recover.add_argument('--config', required=True)
    recover.add_argument('--output', required=True)
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
    dump.add_argument('--destination', help='Export results to a new directory instead of rebuilding in place')
    dump.add_argument('--all-attempts', action='store_true', help='Include retries in destination export (summaries always use latest attempts)')
    listing = commands.add_parser('list', help='List experiments under a host directory')
    listing.add_argument('--root', default='eval-runs')
    for name in ('status', 'results'):
        inspect = commands.add_parser(name, help='Inspect recorded ' + name + ' without contacting guests')
        inspect.add_argument('output')
        if name == 'results':
            inspect.add_argument('--all-attempts', action='store_true')
    logs = commands.add_parser('logs', help='Read collected trial worker and hook logs')
    logs.add_argument('output')
    logs.add_argument('--trial', required=True)
    logs.add_argument('--attempt', type=int)
    logs.add_argument('--lines', type=int, default=100)
    study=commands.add_parser('study',help='Run or summarize a collection of scenario experiments')
    study.add_argument('--config',help='Study YAML with experiment paths')
    study.add_argument('--output',required=True)
    study.add_argument('--resume',action='store_true')
    study.add_argument('--evaluations',nargs='+',help='Summarize existing evaluation directories')
    study.add_argument('--baseline',default='baseline')
    args = parser.parse_args(argv)
    try:
        if args.command=='study':
            from . import studies
            from .storage import write_json
            from pathlib import Path
            if bool(args.config)==bool(args.evaluations):raise ValueError('Specify study config or existing evaluations')
            report=studies.run(args.config,args.output,resume=args.resume) if args.config else studies.summarize(args.evaluations,args.baseline)
            if args.evaluations:write_json(Path(args.output)/'study-summary.json',report)
            print(json.dumps(report,indent=2))
        elif args.command in {'list', 'status', 'results', 'logs'}:
            from . import reporting
            if args.command == 'list':
                result = reporting.list_runs(args.root)
            elif args.command == 'status':
                result = reporting.status(args.output)
            elif args.command == 'results':
                result = reporting.results(args.output, all_attempts=args.all_attempts)
            else:
                result = reporting.logs(args.output, args.trial, attempt=args.attempt, lines=args.lines)
            print(json.dumps(result, indent=2))
        elif args.command in {'fetch-suite', 'check-backend', 'recover'}:
            from pathlib import Path
            from .backends import resolve_backend, create_backend
            from .engine import resolve_engine, runtime_identity
            from .spec import StrictLoader
            path = Path(args.config).resolve()
            config = yaml.load(path.read_text(), Loader=StrictLoader)
            config['backend'] = resolve_backend(config.get('backend', {'type': 'local'}))
            config['engine'] = resolve_engine(config['engine'], path.parent, remote=config['backend']['type'] != 'local')
            backend = create_backend(config)
            if args.command == 'fetch-suite':
                if not backend:
                    raise ValueError('fetch-suite requires the proxmox backend')
                result = backend.fetch_suite(args.guest_path, args.output)
            elif args.command == 'recover':
                if not backend:
                    raise ValueError('recover requires the proxmox backend')
                output = Path(args.output).resolve()
                from .storage import read_json
                manifest = read_json(output / 'manifest.json')
                if manifest['spec'].get('backend', {}).get('participant_vmid') != backend.vmid:
                    raise ValueError('Recovery configuration must select the original participant VM')
                with lease(output / '.coordinator.lock'), backend.lock():
                    backend.recover(output)
                result = {'output': str(output), 'recovered': True}
            else:
                result = backend.identities() if backend else runtime_identity(config['engine'])
            print(json.dumps(result, indent=2))
        elif args.command == 'import-suite':
            from .scenarioforge import import_config
            print(json.dumps(import_config(args.package, args.config, args.output, args.max_readiness_age_seconds), indent=2))
        elif args.command == 'plan':
            spec = resolve(args.spec)
            result = {'backend': spec['backend'], 'engine': spec['engine'], 'spec_hash': digest(spec), 'schedule': schedule(spec)}
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
            rows = run(args.spec, args.output, resume=args.resume, retry_failed=args.retry_failed, progress=lambda message: print(message, flush=True))
            latest = {r['trial_id']: r for r in rows}
            return 0 if all(r['status'] == 'completed' for r in latest.values()) else 1
        elif args.destination:
            from .reporting import export_results
            print(json.dumps(export_results(args.output, args.destination, all_attempts=args.all_attempts), indent=2))
        else:
            if args.all_attempts:
                raise ValueError('--all-attempts requires --destination; in-place export always includes all attempts')
            from pathlib import Path
            if not (Path(args.output) / 'manifest.json').is_file():
                raise ValueError('No experiment manifest in output directory')
            with lease(Path(args.output) / '.coordinator.lock'):
                print(f'Exported {len(export(args.output))} attempts')
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError, zipfile.BadZipFile, CleanupError, PreparationError) as exc:
        print(f'Evaluation error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
