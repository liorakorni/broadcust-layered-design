#!/bin/bash
# Deploy one stage. Mirrors the sibling repos: dev branch -> dev, main -> live.

set -e

AWS_PROFILE="${AWS_PROFILE:-broadcust}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PROFILE AWS_REGION

BRANCH=$(git rev-parse --abbrev-ref HEAD)
STAGE=${1:-dev}

if [[ "$BRANCH" == "dev" && "$STAGE" != "dev" ]]; then
  echo "Deploy to '$STAGE' from 'dev' branch not allowed."
  exit 1
elif [[ "$BRANCH" == "main" && "$STAGE" != "live" ]]; then
  echo "Deploy to '$STAGE' from 'main' branch not allowed."
  exit 1
elif [[ "$BRANCH" != "dev" && "$BRANCH" != "main" ]]; then
  echo "Only 'dev' and 'main' branches may be used for deployment."
  exit 1
fi

if [[ ! -f "config.${STAGE}.json" ]]; then
  echo "Missing config.${STAGE}.json — copy from config.template.json"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running; the stage function is a container image."
  exit 1
fi

echo "Deploying stage '$STAGE' from branch '$BRANCH' (profile $AWS_PROFILE)..."
npx serverless deploy --stage "$STAGE"
