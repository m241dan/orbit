"""Writes the protocol's schema to the path given as the one argument, or ``SCHEMA_PATH``."""

import sys
from pathlib import Path

from orbit.protocol.schema import SCHEMA_PATH, write_schema

write_schema(Path(sys.argv[1]) if len(sys.argv) > 1 else SCHEMA_PATH)
