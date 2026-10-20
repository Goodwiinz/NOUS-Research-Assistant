#!/bin/sh
set -u

python3 /tests/verify.py
status=$?

case "$status" in
  0)
    echo 1 > /logs/verifier/reward.txt
    exit 0
    ;;
  10)
    echo 0 > /logs/verifier/reward.txt
    exit 0
    ;;
  *)
    echo "verifier infrastructure failure (exit $status); no reward emitted" >&2
    exit "$status"
    ;;
esac
