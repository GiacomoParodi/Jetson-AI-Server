"""Avvio del server: python -m app"""
import logging
import os
import tempfile

import uvicorn

from .config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    # I file caricati passano da una cartella temporanea: teniamola sul disco dati.
    tmp = settings.data_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
