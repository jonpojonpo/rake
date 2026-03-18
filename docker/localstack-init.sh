#!/bin/bash
# Create the default results bucket on LocalStack startup.
awslocal s3 mb s3://${S3_RESULTS_BUCKET:-rake-results} --region ${DEFAULT_REGION:-us-east-1} || true
echo "LocalStack: rake-results bucket ready"
