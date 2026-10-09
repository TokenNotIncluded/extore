"""Unprivileged runner entry point for the root-owned deployment socket."""

import re
import socket
import sys


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]):
        raise ValueError("Expected a full lowercase Git commit SHA")
    result = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1400)
        connection.connect("/run/extore-deploy/trigger.sock")
        connection.sendall((sys.argv[1] + "\n").encode("ascii"))
        connection.shutdown(socket.SHUT_WR)
        with connection.makefile("r") as stream:
            for line in stream:
                print(line, end="", flush=True)
                if line.startswith("EXTORE_DEPLOY_RESULT="):
                    result = line.strip().split("=", 1)[1]
    if result not in {"deployed", "current", "superseded"}:
        raise RuntimeError("Deployment did not report a verified result")


if __name__ == "__main__":
    main()
