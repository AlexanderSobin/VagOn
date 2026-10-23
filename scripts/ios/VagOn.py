"""VagOn launcher for Pythonista 3 on iPhone / iPad.

1. Put this file next to the `vagon` folder in Pythonista (see docs/IPHONE.md),
   or run install_vagon.py once to download everything.
2. Run it (▶). The VagOn interface opens full screen inside Pythonista.
   Close it with the ✕ button to stop.

Adapter on iPhone: Bluetooth LE ELM327 ("ble") or Wi-Fi ELM327 ("wifi").
Classic Bluetooth adapters can't be used by iOS apps.
"""

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (HERE, os.path.join(HERE, "VagOn"), os.path.join(HERE, "vagon-main"),
             os.path.join(HERE, "VagOn-main"), os.path.dirname(os.path.dirname(HERE))):
    if os.path.isfile(os.path.join(cand, "vagon", "__init__.py")):
        sys.path.insert(0, cand)
        break
else:
    raise SystemExit("Не найдена папка 'vagon' рядом со скриптом. Запустите install_vagon.py "
                     "или скопируйте папку vagon из репозитория рядом с VagOn.py")

# Pythonista keeps imported modules between runs; drop stale copies after an update
for name in [m for m in sys.modules if m == "vagon" or m.startswith("vagon.")]:
    del sys.modules[name]

from vagon.app import App  # noqa: E402
from vagon.web.server import make_server  # noqa: E402

PORT = 8765
DEFAULT_ADAPTER = os.environ.get("VAGON_ADAPTER", "ble")


def main():
    app = App(DEFAULT_ADAPTER)
    srv = make_server(app, "127.0.0.1", PORT)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="vagon-web")
    t.start()
    url = f"http://127.0.0.1:{PORT}/"
    try:
        import ui  # Pythonista
        wv = ui.WebView(name="VagOn")
        wv.load_url(url)
        wv.present("fullscreen")
        wv.wait_modal()
    except ImportError:
        import time
        import webbrowser
        print("VagOn:", url)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        srv.shutdown()
        srv.server_close()
        app.disconnect()


if __name__ == "__main__":
    main()
