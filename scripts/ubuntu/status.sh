#!/usr/bin/env bash
set -euo pipefail
systemctl status router-portal --no-pager || true
journalctl -u router-portal -n 200 --no-pager || true


