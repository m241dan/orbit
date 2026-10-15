"""Settings for every test."""

import os

# The tab strip's underline slides for 0.3 s on each activation, and the pilot waits for it;
# tests check where it lands, not the slide. Set before Textual is imported, which reads it then.
os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")
