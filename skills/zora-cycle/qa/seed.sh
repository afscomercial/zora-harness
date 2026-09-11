#!/bin/bash
set -euo pipefail
: "${QA_WORK:?}" "${QA_MONGO_URI:?}" "${QA_IDENTITY_FILE:?}"
node "$(dirname "$QA_WORK")/in/seed-baseline.cjs"
# Use the feature commit's existing template seeder rather than duplicate its schema.
export MONGO_TASKS_DB_URL="$QA_MONGO_URI" MONGO_TASKS_DB_NAME=task-service
SEED_TENANT_ID=$(python3 -c 'import json,os; print(json.load(open(os.environ["QA_IDENTITY_FILE"]))["tenant"]["tenantId"])')
export SEED_TENANT_ID
cd "$QA_WORK"
pnpm --filter task-service seed:templates
