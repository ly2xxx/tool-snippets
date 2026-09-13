#!/usr/bin/env python3
"""videograb - find and download the videos behind a web page.

    python videograb.py extract  <url> [options]
    python videograb.py download <url> -o ./out
    python videograb.py login    <url> --storage-state session.json

See README.md, or `python videograb.py --help`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vgrab.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
