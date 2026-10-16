#!/usr/bin/env python3
"""Подделка исполнителя как настоящий процесс: читает stdin, ведёт себя по первому аргументу."""
import sys
import time

mode = sys.argv[1]
data = sys.stdin.read()
if mode == "echo":
    print("получено:" + data)
elif mode == "hang":
    time.sleep(60)
elif mode == "fail":
    print("сломалось", file=sys.stderr)
    sys.exit(3)
