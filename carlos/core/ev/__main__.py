from __future__ import annotations

import argparse
import asyncio
import sys

from .service import AlreadyRunningError, run_service


def main() -> int:
    parser = argparse.ArgumentParser(prog="ev-core", description="E.V. background core")
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(run_service(verbose=arguments.verbose))
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
