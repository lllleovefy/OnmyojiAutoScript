"""Start only the OAS OCR RPC service, without GUI or account configuration."""

import os

from module.ocr.rpc import run_ocr_server


def main() -> None:
    workers = int(os.environ.get("OCR_WORKERS", "2"))
    if workers < 1:
        raise ValueError("OCR_WORKERS must be at least 1")
    run_ocr_server("0.0.0.0", 22268, {"worker_count": workers})


if __name__ == "__main__":
    main()
