"""Headless execution adapter around MCPSession, independent of CLI and Flask.

Run in a dedicated process: CAF_RUN_BASE_DIR and CAF_TOOLS_CONFIG_PATH are
process-scoped configuration shared with the native MCP subprocess.
"""
import asyncio
import json
import os
import threading
import time
from pathlib import Path

from cyber_agent_flow_eval.storage import write_json
from .engine import load_session


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    return json.loads(json.dumps(value, default=str))


class RecordingClient:
    """Capture every engine model call, including summarization and retries."""
    def __init__(self, client, directory):
        self.client = client
        self.directory = Path(directory)
        self.count = 0
        self.lock = threading.Lock()
        self.records = []
        self.storage_errors = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def save(self, path, record):
        try:
            write_json(path, record)
        except Exception as exc:
            self.storage_errors.append(str(exc))
            raise

    def chat(self, *args, **kwargs):
        with self.lock:
            self.count += 1
            number = self.count
        record = {'call': number, 'request': plain({'args': args, 'kwargs': kwargs})}
        path = self.directory / f'call-{number:06d}.json'
        self.save(path, record)
        started = time.monotonic()
        try:
            result = self.client.chat(*args, **kwargs)
            record['response'] = plain(result)
            return result
        except Exception as exc:
            record['error'] = str(exc)
            raise
        finally:
            record['elapsed_seconds'] = time.monotonic() - started
            self.save(path, record)
            self.records.append(record)


async def execute(config, directory):
    directory = Path(directory)
    cancel = asyncio.Event()
    events, errors = [], []
    interaction = False

    def on_event(event):
        nonlocal interaction
        events.append(plain(event))
        # _emit suppresses callback errors, so retain them and fail the result.
        try:
            with (directory / 'events.jsonl').open('a') as stream:
                stream.write(json.dumps(plain(event)) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            errors.append(str(exc))
            cancel.set()
        if event.get('type') == 'error':
            errors.append(event.get('message', 'Engine error'))
        if event.get('type') in {'dangerous_tool_approval', 'post_tool_reply_decision', 'tool_timeout_decision'}:
            interaction = True
            cancel.set()
            if event.get('type') == 'tool_timeout_decision':
                try:
                    session.resolve_tool_timeout_decision('kill')
                except Exception as exc:
                    errors.append(f'Timeout cleanup failed: {exc}')

    model, limits = config['model'], config['execution']
    session = load_session(config['engine'])(
        ollama_url=model['url'], llm_provider=model['provider'], model=model['name'],
        api_key=os.environ.get(model.get('api_key_env', '')), ssl_verify=model.get('ssl_verify', True),
        server_command=config['server_command'], run_id=config['run_id'], event_callback=on_event,
        context_window=limits['context_window'], max_turns=limits['max_turns'],
        tool_timeout=limits['tool_timeout'], network_policy=limits['network_policy'],
        enabled_tool_guides=[], enabled_playbooks=[], allowed_tools=config['tools'],
        guidance_text=config['guidance'], reveal_network_policy=limits.get('reveal_network_policy', False),
    )
    recorder = None
    started = time.monotonic()
    status = 'completed'
    try:
        await session.start()
        write_json(directory / 'checkpoint.json', plain(session.messages))
        recorder = RecordingClient(session._client, directory / 'model_calls')
        session._client = recorder
        await session.chat(config['prompt'], cancel_event=cancel)
        if interaction:
            status = 'interaction_required'
        elif errors:
            status = 'error'
        elif cancel.is_set():
            status = 'cancelled'
    except Exception as exc:
        status = 'error'
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        await session.stop()
        # start() may fail after opening the transport but before _started=True.
        if session._exit_stack:
            await session._exit_stack.aclose()
    if recorder:
        errors.extend(recorder.storage_errors)
    if errors and status == 'completed':
        status = 'error'
    messages = plain(session.messages)
    write_json(directory / 'messages.json', messages)
    final = next((m.get('content', '') for m in reversed(messages)
                  if m.get('role') == 'assistant' and m.get('content')), '')
    return {'status': status, 'final_answer': final, 'errors': errors,
            'elapsed_seconds': time.monotonic() - started,
            'model_calls': recorder.count if recorder else 0,
            'turn_budget_exhausted': any(e.get('type') == 'chat_done' and 'Max iterations' in e.get('message', '') for e in events),
            'provider_usage': [r.get('response', {}).get('raw', r.get('response', {})).get('usage')
                               for r in recorder.records] if recorder else [],
            'usage_complete': False, 'nested_operation_telemetry_complete': False}
