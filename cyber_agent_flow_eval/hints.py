"""Host-owned progressive assistance. Never upload this policy or its facts to CAF."""
import hashlib
import json
import time
from datetime import datetime, timezone
from .storage import write_json, read_json

POLICY = {'version': 1, 'stalled_turns': 2, 'max_hints': 3,
          'progress': 'new declared fact in bounded tool output; otherwise new successful tool output'}


def strings(value):
    if isinstance(value, str):
        yield value
    elif type(value) in (int, float, bool):
        yield json.dumps(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


class HintController:
    def __init__(self, directory, metadata, verifier):
        self.directory, self.verifier = directory, verifier
        self.started = time.monotonic()
        self.facts = metadata.get('discoverable_facts', [])
        self.known = {f['id'] for f in metadata.get('starting_facts', [])}
        self.observed, self.revealed, self.outputs = set(), set(), set()
        self.last_progress = 0
        self.response = None
        self.events = []
        self.hints = []
        answers = list(strings(verifier.get('expected')))
        def safe(text):
            return not any(answer and answer in text for answer in answers)
        for index, text in enumerate(metadata.get('progressive_hints', [])):
            if not isinstance(text, str) or not text.strip() or len(text) > 1500:
                raise ValueError('progressive_hints must contain nonempty strings up to 1500 characters')
            if not safe(text):
                raise ValueError('A progressive hint contains a verifier answer')
            self.hints.append(dict(id=f'authored-{index + 1}', text=text, source='scenario_task'))
        for fact in self.facts:
            requirements = set(fact.get('requires', [])) | set(metadata.get('objective_requires', {}).get(fact.get('source_node'), []))
            # A fact never requires itself to be revealed.
            requirements.discard(fact['id'])
            pointer = f"Look for the {fact['artifact']} needed for the objective."
            evidence = f"For {fact['artifact']}, inspect this evidence: {fact['evidence']}"
            reveal = f"Required fact supplied by the evaluator: {fact['artifact']} = {fact['value']}"
            for stage, text in enumerate((pointer, evidence, reveal), 1):
                if safe(text):
                    self.hints.append(dict(id=f"{fact['id']}-{stage}", text=text, source='scenario_fact',
                        fact_id=fact['id'], stage=stage, requires=sorted(requirements), reveals_fact=stage == 3 or fact['value'] in text))
        if not self.hints:
            raise ValueError('Progressive hints enabled but task has no usable progressive_hints or discoverable_facts')
        self.save()

    def save(self):
        write_json(self.directory / 'assistance.json', dict(policy=POLICY, events=self.events,
                   observed_fact_ids=sorted(self.observed), revealed_fact_ids=sorted(self.revealed)))

    def respond(self, request):
        if not isinstance(request, dict):
            raise ValueError('Invalid hint observation')
        turn = request.get('turn')
        if type(turn) is not int or turn < 1 or request.get('sequence') != turn:
            raise ValueError('Invalid hint sequence')
        if self.response and turn <= self.response['sequence']:
            return self.response
        results = request.get('results', [])
        if not isinstance(results, list) or len(results) > 2 or any(not isinstance(r, str) or len(r) > 1200 for r in results):
            raise ValueError('Invalid hint evidence')
        new_progress = False
        for fact in self.facts:
            if fact['id'] not in self.known and any(fact['value'] in result for result in results):
                self.known.add(fact['id']); self.observed.add(fact['id'])
                new_progress = True
        if not self.facts:
            for result in results:
                digest = hashlib.sha256(result.encode()).hexdigest()
                if result.strip() and digest not in self.outputs:
                    new_progress = True
                self.outputs.add(digest)
        if new_progress:
            self.last_progress = turn
        final = request.get('final_answer')
        failed_final = False
        if final is not None:
            from .runner import verify
            # Do not interrupt a correct final answer or judge truncated answers.
            if request.get('final_truncated') or verify(final, self.verifier)['passed']:
                self.response = dict(sequence=turn, hint=None)
                self.save()
                return self.response
            failed_final = True
        hint = None
        if len(self.events) < POLICY['max_hints'] and (failed_final or turn - self.last_progress >= POLICY['stalled_turns']):
            used = {e['id'] for e in self.events}
            for candidate in self.hints:
                if candidate['id'] in used or candidate.get('fact_id') in self.known or not set(candidate.get('requires', [])) <= self.known:
                    continue
                hint = candidate['text']
                event = dict(candidate, turn=turn, at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - self.started,
                             reason='incorrect_final_answer' if failed_final else 'no_observed_progress')
                self.events.append(event)
                if candidate.get('reveals_fact'):
                    self.known.add(candidate['fact_id']); self.revealed.add(candidate['fact_id'])
                self.last_progress = turn
                break
        self.response = dict(sequence=turn, hint=hint)
        self.save()
        return self.response

    def poll(self):
        path = self.directory / 'hint-request.json'
        if path.exists():
            write_json(self.directory / 'hint-response.json', self.respond(read_json(path)))


def metrics(directory, enabled, success):
    path = directory / 'assistance.json'
    audit = read_json(path) if path.exists() else {}
    events = audit.get('events', [])
    return dict(provide_progressive_hints=enabled, hints_released=len(events),
                facts_revealed=len(audit.get('revealed_fact_ids', [])), assistance= audit,
                assisted_success=success is True and bool(events),
                unassisted_success=success is True and not events)
