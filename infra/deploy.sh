#!/usr/bin/env bash
# deploy.sh — one-shot deployment of the entire rake Azure stack
#
# Prerequisites:
#   az login
#   docker login <acr>.azurecr.io
#
# Usage:
#   ./infra/deploy.sh --suffix abc123 --api-key sk-ant-...
#   ./infra/deploy.sh --suffix abc123 --api-key sk-ant-... --location westeurope

set -euo pipefail

SUFFIX=""
API_KEY=""
LOCATION="eastus"
IMAGE_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo 'latest')"
RESOURCE_GROUP="rg-rake"

usage() {
  echo "Usage: $0 --suffix <suffix> --api-key <key> [--location <region>] [--tag <tag>]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --suffix)   SUFFIX="$2";    shift 2 ;;
    --api-key)  API_KEY="$2";   shift 2 ;;
    --location) LOCATION="$2";  shift 2 ;;
    --tag)      IMAGE_TAG="$2"; shift 2 ;;
    *)          usage ;;
  esac
done

[[ -z "$SUFFIX" || -z "$API_KEY" ]] && usage

ACR_NAME="acrake${SUFFIX}"
IMAGE_REPO="${ACR_NAME}.azurecr.io/rake"

echo "==> Creating resource group $RESOURCE_GROUP in $LOCATION"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (Bicep)"
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$(dirname "$0")/main.bicep" \
  --parameters suffix="$SUFFIX" \
               location="$LOCATION" \
               anthropicApiKey="$API_KEY" \
               imageTag="$IMAGE_TAG" \
  --output none

echo "==> Building and pushing Docker image"
ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
az acr login --name "$ACR_NAME"

docker build \
  --file "$(dirname "$0")/../docker/Dockerfile" \
  --tag "${IMAGE_REPO}:${IMAGE_TAG}" \
  --tag "${IMAGE_REPO}:latest" \
  "$(dirname "$0")/.."

docker push "${IMAGE_REPO}:${IMAGE_TAG}"
docker push "${IMAGE_REPO}:latest"

echo "==> Updating Container Apps with new image"
for APP in rake-code-review rake-security-audit rake-data-analysis; do
  az containerapp update \
    --name "$APP" \
    --resource-group "$RESOURCE_GROUP" \
    --image "${IMAGE_REPO}:${IMAGE_TAG}" \
    --output none
  echo "    Updated $APP"
done

echo ""
echo "==> Deployment complete!"
echo ""
az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name main \
  --query properties.outputs \
  --output table
