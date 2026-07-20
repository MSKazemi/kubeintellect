#!/usr/bin/env python3
"""
Trim an asciinema .cast file.

Usage:
  python trim_cast.py input.cast output.cast --start 3.5
  python trim_cast.py input.cast output.cast --start 3.5 --end 120.0

Options:
  --start SECONDS   Cut everything before this timestamp (default: 0)
  --end   SECONDS   Cut everything after this timestamp (default: end of file)
  --speed FACTOR    Speed up/slow down playback (e.g. 1.5 = 50% faster)
  --max-idle SECS   Cap silence gaps to this many seconds (default: no cap)
"""

import argparse
import json
import sys


def trim(input_path: str, output_path: str, start: float, end: float | None,
         speed: float, max_idle: float | None) -> None:
    with open(input_path) as f:
        lines = f.readlines()

    if not lines:
        print("Error: empty cast file", file=sys.stderr)
        sys.exit(1)

    header = json.loads(lines[0])
    events = [json.loads(l) for l in lines[1:] if l.strip()]

    # Filter by time window
    events = [e for e in events if e[0] >= start]
    if end is not None:
        events = [e for e in events if e[0] <= end]

    if not events:
        print("Error: no events in the selected time range", file=sys.stderr)
        sys.exit(1)

    # Re-zero timestamps relative to start
    offset = events[0][0]
    events = [[round((e[0] - offset) / speed, 6), e[1], e[2]] for e in events]

    # Cap idle gaps
    if max_idle is not None:
        prev_ts = 0.0
        adjusted = []
        accumulated_cut = 0.0
        for e in events:
            gap = e[0] - prev_ts
            if gap > max_idle:
                accumulated_cut += gap - max_idle
                e[0] = round(prev_ts + max_idle, 6)
            else:
                e[0] = round(e[0] - accumulated_cut, 6)
            prev_ts = e[0]
            adjusted.append(e)
        events = adjusted

    with open(output_path, "w") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")

    duration = events[-1][0] if events else 0
    print(f"Written {len(events)} events to {output_path} ({duration:.1f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Input .cast file")
    parser.add_argument("output", help="Output .cast file")
    parser.add_argument("--start", type=float, default=0.0,
                        help="Cut everything before this timestamp (seconds)")
    parser.add_argument("--end", type=float, default=None,
                        help="Cut everything after this timestamp (seconds)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Playback speed multiplier (e.g. 1.5 = faster)")
    parser.add_argument("--max-idle", type=float, default=None,
                        help="Cap silence gaps to this many seconds")
    args = parser.parse_args()

    trim(args.input, args.output, args.start, args.end, args.speed, args.max_idle)


if __name__ == "__main__":
    main()
