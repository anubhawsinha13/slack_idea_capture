#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python scripts/slack_bot.py
