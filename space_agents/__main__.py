import logging
import os
import sys

from space_agents.main import run

code = run()
# All cleanup (core loop, database, document worker) has run inside run(). Exit hard: pywebview executes bridge calls
# on non-daemon threads that wait for the window to acknowledge their return value, so a call that was in flight
# when the window closed would otherwise block interpreter shutdown forever.
logging.shutdown()
sys.stdout.flush()
sys.stderr.flush()
os._exit(code)
