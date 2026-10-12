"""groknmok self-update: replace this program with the latest signed release
(filled in by build step 8; for now only what the timer needs)."""

import os
import sys
import zipfile


def program_path():
    """Path of the zipapp this program runs from, or None when it runs from source."""
    import groknmok
    archive = os.path.dirname(os.path.dirname(os.path.abspath(groknmok.__file__)))
    return archive if os.path.isfile(archive) and zipfile.is_zipfile(archive) else None


def notify_if_available(p):
    return None


def cmd(argv):
    sys.exit("groknmok self-update: not available yet")
