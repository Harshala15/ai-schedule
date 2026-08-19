# Windy Capture Automation

This project is now a **capture-only** Windy automation tool.

It uses Playwright to:
- log into Windy Premium
- capture screenshots for the configured layers
- record the satellite animation clip

## Main file

- [test_multi_image.py](test_multi_image.py) is the only important runtime script.

## What it does

1. Opens Windy using your saved session from `windy_login.json`
2. Captures screenshots for the configured layers
3. Records the satellite animation
4. Repeats on the configured interval

## Setup

### Requirements

- Python 3.11+
- Playwright
- boto3
- ffmpeg
- Windy Premium account

### Install

```bash
pip install playwright boto3
playwright install chromium
```

## Configure

Update [config.py](config.py) if you want to change:
- plant name
- latitude / longitude
- viewport size
- zoom level
- capture interval
- screenshot/video folders
- S3 bucket name/prefix/region

For S3 uploads, bucket names must be lowercase and use only valid S3 characters. The default bucket name is `ai-forecasting-storage`, and you can override it with `S3_BUCKET_NAME`.

If you want to use your downloaded access keys, set standard AWS environment variables before running:

```bash
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...   # only if your credentials include a session token
AWS_DEFAULT_REGION=ap-south-1
```

On EC2, an instance role with S3 permission also works, and boto3 will use it automatically.

If you are running with Docker Compose, put those variables in `.env` in the project root and `docker compose up` will pass them into the container.

## Run locally

```bash
python test_multi_image.py
```

## Docker / EC2

Build:

```bash
docker build -t windy-capture .
```

Run:

```bash
docker compose up -d
docker logs -f windy-capture
```

The helper script [ec2_start.sh](ec2_start.sh) checks for `windy_login.json`, creates the output folders, and starts the container.

## Lambda container

Lambda should run one site capture and exit. Use EventBridge to trigger each
site function on a frequent schedule instead of running the script's infinite
loop.

Build the Lambda image:

```bash
docker build -f Dockerfile.windy-capture-lambda -t windy-capture-lambda .
```

Deploy the image through ECR to an AWS Lambda function with handler:

```text
test_multi_image.lambda_handler
```

If you want a dedicated BHUPALPALLY Lambda, use this handler instead:

```text
bhupalpally_lambda.lambda_handler
```

Configure the Lambda function with:

- IAM execution role with S3 permissions for the configured bucket
- timeout high enough for one capture cycle, up to Lambda's 15 minute maximum
- memory around 4096 MB or higher for Playwright/Chromium
- ephemeral storage large enough for the video capture and metadata, for example 2048 MB+
- environment variables such as `SITE_ID=SIRMOUR`, `SITE_ID=KASIPET`, `SITE_ID=BHUPALPALLY`, `SITE_ID=OSEPL`, `PLANT_ID=vedanjay`, and `S3_BUCKET=ai-forecasting-test-755611554012`

For the dedicated BHUPALPALLY Lambda, `SITE_ID` is forced in code, so the
event payload can stay minimal.

The Lambda handler captures the configured `SITE_ID`, writes working files to
`/tmp`, and uploads to:

```text
raw/<PLANT_ID>/<SITE_ID>/<DATE>/windy/videos/
raw/<PLANT_ID>/<SITE_ID>/<DATE>/windy/metadata/
```

The Lambda image includes `windy_login.json`, because Lambda cannot mount the
EC2 Docker volume used by `docker-compose.yml`. Keep `.env` out of the image
and use the Lambda IAM role for S3 access. Current valid `SITE_ID` values come
from `config.py`: `SIRMOUR`, `KASIPET`, `BHUPALPALLY`, and `OSEPL`.

Lambda no longer captures the five screenshot layers. It only records the
satellite animation video and writes metadata for the run. Screenshot capture
remains in the local/EC2 flow.

For SIRMOUR, the Lambda only runs when the current IST time is 5 minutes
before one of these revision times:
`05:15`, `06:45`, `08:15`, `09:45`, `11:15`, `14:15`, `15:45`.
If the Lambda is invoked outside those capture minutes, it exits cleanly
without recording video.

## Important note

The first Windy login is interactive. After you log in once, save the generated `windy_login.json` and reuse it for future headless runs.
