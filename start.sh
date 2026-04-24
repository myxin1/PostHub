#!/bin/sh
set -eu

if [ "${POSTHUB_PROCESS:-web}" = "worker" ]; then
  exec python backend/worker_runner.py
fi

exec python backend/server.py
