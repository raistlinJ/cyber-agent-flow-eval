"""Mirror returned CAF events on the host without an extra guest poll loop."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path

MAX_EVENT_BYTES = 48 * 1024
MAX_PENDING_BYTES = 256 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024


class LiveTranscript:
    def __init__(self, directory):
        directory = Path(directory)
        root = directory.parents[2] if directory.parent.parent.name == 'trials' else directory
        self.path = root / 'live-transcript.jsonl'
        self.offset = 0
        self.pending = bytearray()
        self.discarding = False
        try:
            row = json.loads((directory / 'attempt.json').read_text())
        except FileNotFoundError:
            row = {}
        self.identity = {key: row.get(key) for key in ('trial_id', 'attempt', 'condition_id', 'task_id')}

    def emit(self, event):
        if not isinstance(event, dict):
            return
        record = dict(self.identity, at=datetime.now(timezone.utc).isoformat(), event=event)
        content = json.dumps(record, ensure_ascii=False).encode()
        if len(content) > MAX_EVENT_BYTES:
            record['event'] = {'type': event.get('type', 'output'), 'text': json.dumps(event, ensure_ascii=False)[:8000],
                               'truncated': True, 'message': 'Live preview truncated; full output is preserved in the run bundle.'}
            content = json.dumps(record, ensure_ascii=False).encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            if os.fstat(stream.fileno()).st_size > MAX_JOURNAL_BYTES:
                raise ValueError('Live transcript preview limit reached; full logs remain in the run bundle')
            stream.write(content + b'\n')

    def feed(self, content):
        # Keep incomplete JSON/UTF-8 lines until the next acknowledged chunk.
        self.pending.extend(content)
        while b'\n' in self.pending:
            line, _, rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            if self.discarding:
                self.discarding = False
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                event = {'type': 'status', 'message': 'An unreadable live event was skipped; consult the full collected logs.'}
            self.emit(event)
        if len(self.pending) > MAX_PENDING_BYTES:
            if not self.discarding:
                self.emit({'type': 'status', 'message': 'An oversized live event was skipped; full output will be collected in the run bundle.'})
            self.pending.clear()
            self.discarding = True

    def accept(self, chunk):
        if not isinstance(chunk, dict) or chunk.get('error'):
            return
        content = base64.b64decode(chunk['content'], validate=True)
        if len(content) > 16 * 1024 or chunk['offset'] != self.offset:
            raise ValueError('Invalid live transcript chunk')
        self.feed(content)
        self.offset += len(content)

    def finish(self, path):
        if not path.is_file():
            return
        with path.open('rb') as stream:
            stream.seek(self.offset)
            while content := stream.read(16 * 1024):
                self.feed(content)
                self.offset += len(content)
