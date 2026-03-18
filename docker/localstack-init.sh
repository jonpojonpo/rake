#!/bin/bash
# LocalStack initialization — creates S3 buckets and SQS queues on startup.
set -e

AWS="aws --endpoint-url=http://localhost:4566 --region us-east-1"

echo "Creating S3 buckets..."
$AWS s3 mb s3://rake-uploads-local    2>/dev/null || true
$AWS s3 mb s3://rake-results-local    2>/dev/null || true

echo "Creating SQS queues..."
$AWS sqs create-queue --queue-name rake-doc-analysis    2>/dev/null || true
$AWS sqs create-queue --queue-name rake-data-analysis   2>/dev/null || true
$AWS sqs create-queue --queue-name rake-doc-dlq         2>/dev/null || true

echo "Creating SNS topic..."
$AWS sns create-topic --name rake-findings 2>/dev/null || true

echo "LocalStack resources ready."
