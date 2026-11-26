"""Entry point: python3 -m megadl"""

import logging
import os
import signal
import sys
import threading

from . import __version__, config
from .manager import Manager
from .megacmd import MegaCmd
from .store import Store
from .web import create_app


def main():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("megadl")
    log.info("MegaDL %s starting (downloads root: %s)", __version__, config.DOWNLOAD_ROOT)

    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    if not os.path.isdir(config.DOWNLOAD_ROOT):
        log.warning("Downloads root %s does not exist; creating it", config.DOWNLOAD_ROOT)
        os.makedirs(config.DOWNLOAD_ROOT, exist_ok=True)

    store = Store(os.path.join(config.CONFIG_DIR, "megadl.db"))
    mega = MegaCmd(home=config.CONFIG_DIR, bin_dir=config.MEGACMD_BIN_DIR,
                   log_dir=os.path.join(config.CONFIG_DIR, "logs"))
    manager = Manager(mega, store)
    manager.start()
    app = create_app(manager)

    stopping = threading.Event()

    def shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("Received signal %s, shutting down", signum)
        manager.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed; using Flask's development server")
        app.run(host="0.0.0.0", port=config.WEBUI_PORT, threaded=True)
    else:
        log.info("Web GUI listening on port %s", config.WEBUI_PORT)
        serve(app, host="0.0.0.0", port=config.WEBUI_PORT, threads=8, ident="MegaDL")


if __name__ == "__main__":
    main()
