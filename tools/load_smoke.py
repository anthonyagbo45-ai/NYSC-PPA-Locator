#!/usr/bin/env python3
"""Bounded read-only HTTP smoke load. Run only against an authorised test target."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import statistics
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='A directory search URL on your authorised staging/test app')
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        parser.error('Use an HTTP(S) URL without credentials.')
    if not 1 <= args.requests <= 1000 or not 1 <= args.workers <= 32:
        parser.error('Use 1–1000 requests and 1–32 workers.')
    if 'google' in parse_qs(parsed.query):
        parser.error('Benchmark the saved directory, not paid Google discovery.')

    def request(_):
        start = time.perf_counter()
        try:
            with urlopen(args.url, timeout=15) as response:
                response.read()
                code = response.status
        except HTTPError as error:
            code = error.code
        except (URLError, TimeoutError):
            code = 0
        return (time.perf_counter() - start) * 1000, code

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(request, range(args.requests)))
    elapsed = time.perf_counter() - start
    timings = sorted(duration for duration, _ in results)
    failed = sum(code != 200 for _, code in results)
    print(json.dumps({'requests': args.requests, 'workers': args.workers, 'failed': failed,
                      'elapsed_seconds': round(elapsed, 3), 'requests_per_second': round(args.requests / elapsed, 2),
                      'median_ms': round(statistics.median(timings), 2),
                      'p95_ms': round(timings[math.ceil(len(timings) * .95) - 1], 2)}))
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
