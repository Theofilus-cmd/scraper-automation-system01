#!/usr/bin/env bash
#
# Destroys the local dev stack's containers AND volumes (Postgres data,
# Redis data, MinIO data). Prompts for confirmation first since this is
# destructive and not reversible.

set -euo pipefail

read -r -p "This will remove all containers and volumes for this project (all local data will be lost). Continue? [y/N] " confirm

case "${confirm}" in
  [yY][eE][sS]|[yY])
    docker compose down -v
    echo "Done. Run 'make up' to start fresh."
    ;;
  *)
    echo "Aborted. No changes made."
    exit 1
    ;;
esac
