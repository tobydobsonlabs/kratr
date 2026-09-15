"""Packaging entry point.

PyInstaller runs its entry script as ``__main__`` with no package context, so pointing
it straight at ``crate/__main__.py`` breaks that module's relative imports. This
imports the package properly and hands over.
"""

import sys

from crate.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
