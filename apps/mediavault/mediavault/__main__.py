"""Позволяет запускать приложение командой `python3 -m mediavault`."""
import sys

from .main import main

sys.exit(main())
