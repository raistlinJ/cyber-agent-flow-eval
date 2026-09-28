"""Illustrative added tool, not an artifact produced by a CAF generation run.

Fetch at most six same-origin linked pages and report FLAG{...} strings. The
catalog embeds this source so no separate executable must be staged in the guest.
"""
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: http_flag_walk URL')
    root = sys.argv[1]
    origin = urlsplit(root)
    if origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password:
        raise SystemExit('Provide an HTTP(S) URL without credentials')
    pending, seen, flags, errors = [root], set(), set(), []
    opener = build_opener(NoRedirect())
    while pending and len(seen) < 6:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            with opener.open(url, timeout=5) as response:
                body = response.read(65536).decode('utf-8', errors='replace')
            flags.update(re.findall(r'FLAG\{[^{}\s]+\}', body))
            for href in re.findall(r'href=["\x27]([^"\x27]+)["\x27]', body, flags=re.IGNORECASE):
                child = urldefrag(urljoin(url, href))[0]
                parsed = urlsplit(child)
                if (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc) and child not in seen:
                    pending.append(child)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            errors.append({'url': url, 'error': str(exc)})
    print(json.dumps({'flags': sorted(flags), 'pages_read': len(seen), 'errors': errors}))


if __name__ == '__main__':
    main()
