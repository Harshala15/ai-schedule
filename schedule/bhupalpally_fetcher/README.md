# Bhupalpally Fetcher

Bhupalpally-specific FTP downloader Lambda package.

## Output S3 layout

```text
raw/vedanjay/BHUPALPALLY/YYYY-MM-DD/meter_data/<filename>
```

## Lambda env vars

```text
SFTP_HOST
SFTP_PORT
SFTP_USERNAME
SFTP_PASSWORD
SFTP_REMOTE_DIR=/incoming/powerdata_realtime/
SFTP_FILENAME_PREFIX=bhupalpally_
S3_BUCKET
S3_PREFIX_BASE=raw/vedanjay/BHUPALPALLY
```

For local testing, copy [.env.example](./.env.example) to `.env` in the same folder.
Bhupalpally uses the same FTP host and account as Kasipet in this setup; the plant-specific part is the filename prefix (`bhupalpally_`) and the S3 prefix (`raw/vedanjay/BHUPALPALLY`).

The fetcher also falls back to the repo-root `.env` if the package-local `.env` is missing, which makes it easier to share the same credentials during local testing.

## Lambda handler

Use this handler when creating the AWS Lambda function:

```text
bhupalpally_fetcher.lambda_handler.lambda_handler
```

The Lambda role only needs permission to upload into the plant prefix:

```text
arn:aws:s3:::ai-forecasting-storage-429694361053/raw/vedanjay/BHUPALPALLY/*
```

If you want the exact IAM policy JSON, use the repo-root file:

```text
bhupalpally-fetcher-s3-policy.json
```

and attach it to the Lambda execution role together with the standard Lambda trust policy.

## Suggested Lambda env vars

Set these in the Lambda configuration:

```text
SFTP_HOST=ftp.enercast.de
SFTP_PORT=21
SFTP_USERNAME=adani_mundra_solar
SFTP_PASSWORD=<secret>
SFTP_REMOTE_DIR=/incoming/powerdata_realtime/
SFTP_FILENAME_PREFIX=bhupalpally_
S3_BUCKET=ai-forecasting-storage-429694361053
S3_PREFIX_BASE=raw/vedanjay/BHUPALPALLY
```

## Run locally

```bash
python -m bhupalpally_fetcher.lambda_handler
```

## Container image deployment

Build from the repo root:

```bash
docker build -t bhupalpally-fetcher -f bhupalpally_fetcher/Dockerfile .
```

Then push the image to ECR and point the Lambda function at that image.
