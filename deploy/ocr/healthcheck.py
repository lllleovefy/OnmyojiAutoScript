"""Check RPC responsiveness without importing OAS logging or configuration."""

import zerorpc


def main() -> None:
    client = zerorpc.Client(timeout=5)
    try:
        client.connect("tcp://127.0.0.1:22268")
        if client.ping() is not True:
            raise RuntimeError("OCR ping failed")
    finally:
        client.close()


if __name__ == "__main__":
    main()
