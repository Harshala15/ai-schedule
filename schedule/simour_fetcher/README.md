# SIMOUR Fetcher

Small SFTP downloader that pulls the newest file from the SIMOUR / Enercast server and stores it locally.

## Install

```bash
pip install paramiko
```

## Configure

Prefer environment variables:

```bash
set SFTP_HOST=transfer.enercast.de
set SFTP_PORT=22
set SFTP_USERNAME=vedanjay
set SFTP_PASSWORD=your_password_here
set SFTP_REMOTE_DIR=/incoming/powerdata_realtime/SIRMOUR/
set SFTP_LOCAL_DIR=simour_fetcher/downloads
```

## Run

```bash
python -m simour_fetcher.fetch_latest_sftp
```

You can also override values on the command line with `--host`, `--port`, `--username`, `--password`,
`--remote-dir`, and `--local-dir`.

## Lambda

For AWS Lambda, use:

```bash
python -m simour_fetcher.lambda_handler
```

Lambda expects these environment variables:

```text
SFTP_HOST=transfer.enercast.de
SFTP_PORT=22
SFTP_USERNAME=vedanjay
SFTP_PASSWORD=...
SFTP_REMOTE_DIR=/incoming/powerdata_realtime/SIRMOUR/
S3_BUCKET=ai-forecasting-storage-429694361053
S3_PREFIX_BASE=raw/vedanjay/SIRMOUR
```

For local testing, you can also put the same keys in [`simour_fetcher/.env`](/C:/Users/HP/Downloads/SIRMOUR_forecasting_code/simour_fetcher/.env); the Lambda handler reads that file first and then falls back to environment variables.

The uploaded object key is built as:

```text
raw/vedanjay/SIRMOUR/YYYY-MM-DD/meter_data/<filename>
```

This keeps historical files from previous dates intact, and also keeps multiple snapshots from the same day instead of overwriting them.

The Lambda code downloads to `/tmp`, then uploads to S3 using the Lambda execution role.

## Container image deployment

If you want Lambda to run this as a container image:

1. Build the image from the repo root:

```bash
docker build -t simour-fetcher -f simour_fetcher/Dockerfile .
```

2. Authenticate Docker to ECR and create a repository named `simour-fetcher`:

```bash
aws ecr create-repository --repository-name simour-fetcher --region ap-south-1 --profile simour
aws ecr get-login-password --region ap-south-1 --profile simour | docker login --username AWS --password-stdin <aws_account_id>.dkr.ecr.ap-south-1.amazonaws.com
```

3. Tag and push the image:

```bash
docker tag simour-fetcher:latest <aws_account_id>.dkr.ecr.ap-south-1.amazonaws.com/simour-fetcher:latest
docker push <aws_account_id>.dkr.ecr.ap-south-1.amazonaws.com/simour-fetcher:latest
```

4. Create a Lambda function with package type `Image` and point it at that ECR image.

5. Add an EventBridge Scheduler rule with:

```text
cron(3/15 * * * ? *)
```

and timezone:

```text
Asia/Kolkata
```

That gives you runs every 15 minutes at `:03`, `:18`, `:33`, and `:48` IST.

## Container image for Lambda

This folder now includes a Lambda container-image build:

```bash
docker build -t simour-fetcher ./simour_fetcher
```

The image uses the AWS Lambda Python base image and installs `paramiko` inside the image.
