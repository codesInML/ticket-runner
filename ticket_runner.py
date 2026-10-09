#!/usr/bin/env python3
"""Launch the parallel GitHub ticket runner. See --help for workflow details."""

from ticket_runner_lib.cli import entrypoint


if __name__ == "__main__":
    entrypoint()
