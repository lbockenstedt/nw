"""Transport IO for the nw device drivers.

Submodules isolate blocking/vendored IO (pysnmp, asyncssh, httpx) from
``nw_engine.py``. Heavy libs are lazy-imported inside functions so the modules
import cleanly in environments where the lib isn't installed (e.g. the test
env), and blocking calls run off the spoke's event loop via ``asyncio.to_thread``.
"""