"""Bounded SSE/NDJSON completions and private, incremental judge output."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import time

MAX_BYTES = 2_000_000


class EventLog:
    def __init__(self, path):
        self.path = Path(path)
        self.started = self.last_flush = time.monotonic()
        self.pending = {}
        self.context = {}
        self.disabled = False
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
        except OSError:
            self.disabled = True

    def emit(self, kind, **fields):
        if self.disabled:
            return
        event = dict(type=kind, at=datetime.now(timezone.utc).isoformat(),
                     _eval_elapsed_seconds=round(time.monotonic()-self.started, 3),
                     **self.context, **fields)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(fd, 'w') as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                stream.flush()
        except OSError:
            self.disabled = True  # Optional live output must not change scoring.

    def delta(self, channel, text):
        if not isinstance(text, str) or not text:
            return
        self.pending[channel] = self.pending.get(channel, '') + text
        if time.monotonic()-self.last_flush >= .5 or sum(map(len,self.pending.values())) >= 1024:
            self.flush()

    def flush(self):
        for channel, text in self.pending.items():
            self.emit(channel, text=text)
        self.pending.clear()
        self.last_flush = time.monotonic()


def completion_response(response, ollama, callback, deadline):
    """Accept genuine streaming or a server that ignores stream=true."""
    media = response.headers.get('Content-Type', '').lower()
    streaming = 'text/event-stream' in media or 'ndjson' in media
    if not streaming:
        raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Judge response exceeded the size limit')
        data = json.loads(raw)
        message = data['message'] if ollama else data['choices'][0]['message']
        text = message.get('content') or ''
        if callback:
            callback('reasoning', message.get('reasoning_content') or message.get('reasoning') or message.get('thinking'))
            callback('response', text)
        usage = usage_of(data, ollama)
        return text, usage
    parts, usage, total, finished = [], {}, 0, False
    while True:
        remaining = deadline-time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise TimeoutError('Judge time budget exceeded')
        # urllib's socket timeout is per read. Tighten it to the total remaining
        # budget so heartbeats cannot keep a completion alive indefinitely.
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        line = response.readline(min(64*1024,MAX_BYTES-total)+1)
        total += len(line)
        if total > MAX_BYTES or len(line) > 64*1024:
            raise ValueError('Judge response exceeded the size limit')
        if not line:
            break
        line = line.strip()
        if not line or line.startswith(b':'):
            continue
        if line.startswith(b'data:'):
            line = line[5:].strip()
        elif not ollama:
            continue  # SSE event/id/retry metadata.
        if line == b'[DONE]':
            finished = True
            break
        data = json.loads(line)
        if data.get('error'):
            raise ValueError('Judge provider reported a stream error')
        usage.update({k:v for k,v in usage_of(data,ollama).items() if v is not None})
        if ollama:
            message = data.get('message', {})
            finished = bool(data.get('done'))
        else:
            choices = data.get('choices', [])
            if not choices:
                continue  # Usage-only terminal chunk.
            choice = choices[0]
            message = choice.get('delta', {})
            finished |= choice.get('finish_reason') is not None
        text = message.get('content') or ''
        reasoning = message.get('reasoning_content') or message.get('reasoning') or message.get('thinking')
        if text:
            if not isinstance(text,str):
                raise ValueError('Invalid streamed content')
            parts.append(text)
        if callback:
            callback('reasoning', reasoning)
            callback('response', text)
        if ollama and finished:
            break
    if not finished or not parts:
        raise ValueError('Judge stream ended before a complete response')
    return ''.join(parts), usage


def usage_of(data, ollama):
    return ({'prompt_tokens':data.get('prompt_eval_count'),'output_tokens':data.get('eval_count')}
            if ollama else {'prompt_tokens':data.get('usage',{}).get('prompt_tokens'),
                            'output_tokens':data.get('usage',{}).get('completion_tokens')})
