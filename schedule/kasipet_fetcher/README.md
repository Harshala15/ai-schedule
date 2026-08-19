# Kasipet Fetcher

Kasipet-specific FTP downloader Lambda package.

## Output S3 layout

```text
raw/vedanjay/KASIPET/YYYY-MM-DD/meter_data/<filename>
```

## Lambda env vars

```text
SFTP_HOST
SFTP_PORT
SFTP_USERNAME
SFTP_PASSWORD
SFTP_REMOTE_DIR=/incoming/powerdata_realtime/
SFTP_FILENAME_PREFIX=kasipet_
S3_BUCKET
S3_PREFIX_BASE=raw/vedanjay/KASIPET
```

For local testing, copy [.env.example](./.env.example) to `.env` in the same folder and fill in the password.

## Import a historical ZIP bundle

If you have a local archive like `KASIPET.zip` that contains dated CSVs, you can seed S3 with:

```bash
python -m kasipet_fetcher.import_kasipet_zip --archive C:\Users\HP\Downloads\KASIPET.zip --bucket <your-s3-bucket>
```

Each CSV is uploaded to:

```text
raw/vedanjay/KASIPET/YYYY-MM-DD/meter_data/<filename>
```

Use `--dry-run` first if you want to verify the mapping without uploading anything.

If you have individual Kasipet daily exports and want to update the local
historical store used by the feedback loop, you can merge them into:

```bash
python -m kasipet_fetcher.merge_meter_scada_kasipet C:\Users\HP\Downloads\kasipet_20260807.csv C:\Users\HP\Downloads\kasipet_20260808.csv C:\Users\HP\Downloads\kasipet_20260809.csv C:\Users\HP\Downloads\kasipet_20260810.csv
```

That updates `historic_cases/KASIPET/merged_scada_data.csv` and archives the
source files under `historic_cases/KASIPET/raw/`.
