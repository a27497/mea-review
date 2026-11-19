import argparse
import json
import sys
from pathlib import Path
from .contract import decode
from .review import review
from .export import markdown


def main():
    parser = argparse.ArgumentParser(description='MEA offline static PR review; no model or target execution')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('review')
    run.add_argument('snapshot', type=Path)
    run.add_argument('--format', choices=['json', 'markdown'], default='json')
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8810)
    serve.add_argument('--task-dir', type=Path, help='Local SQLite task directory')
    # 0.0.0.0 is for a container whose port is published on the host loopback only. Requests must
    # still carry Host 127.0.0.1:PORT, so the container port must equal the published port.
    serve.add_argument('--bind', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1', help='Listen address')
    args = parser.parse_args()
    try:
        if args.command == 'serve':
            from .server import serve as start
            start(args.port, args.task_dir, args.bind)
        else:
            with args.snapshot.open('rb') as handle:
                data = decode(handle.read(2_000_001))
            report = review(data)
            print(markdown(report) if args.format == 'markdown' else json.dumps(report, ensure_ascii=False, indent=2))
    except (ValueError, OSError, RecursionError, UnicodeError):
        print('MEA input rejected or unavailable. Check the snapshot schema, size and hashes.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
