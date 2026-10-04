# BigQuery → GCS Cloud Function

This project creates a Google Cloud Function that exports a BigQuery table to a Cloud Storage bucket. It supports a source table in a different GCP project than the project that hosts the function.

## Run locally with Docker

Build and start the container from this directory:

```powershell
docker build -t bigquery-gcs-export .
docker run --rm -p 8080:8080 bigquery-gcs-export
```

In another terminal, send a request without the required fields to check that the local HTTP handler responds. This smoke test does not call Google Cloud and should return HTTP 400:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8080 -ContentType 'application/json' -Body '{}'
```

For a real export, authenticate locally and mount your Application Default Credentials into the container:

```powershell
gcloud auth application-default login
docker run --rm -p 8080:8080 `
  -v "$env:APPDATA\gcloud:/root/.config/gcloud:ro" `
  -e GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json `
  bigquery-gcs-export
```

Then POST the same JSON body shown in [Call the function](#4-call-the-function) to `http://localhost:8080`. The authenticated identity needs the BigQuery and Storage permissions listed below.

## 1) Prerequisites

Make sure you have:

- Google Cloud SDK installed
- A GCP project where the Cloud Function will run
- Access to the source BigQuery project
- A storage bucket in the destination project

Login and set your project:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

## 2) Set your deployment values

Open `deploy.ps1` and edit the variables at the top:

```powershell
$ProjectId = 'your-project-id'
$Region = 'us-central1'
$SourceProject = 'source-bigquery-project-id'
$DatasetId = 'your_dataset'
$TableId = 'your_table'
$DestBucket = 'your-project-id-export-bucket'
$DestObject = 'exports/your_table.csv'
$Location = 'US'
$Format = 'CSV'
$AllowUnauthenticated = 'false'
```

If you want the function to be publicly callable, set:

```powershell
$AllowUnauthenticated = 'true'
```

## 3) Deploy the function

From the project folder in PowerShell:

```powershell
.\deploy.ps1
```

This script will:

- enable the required GCP APIs
- create a service account if needed
- grant BigQuery and Storage permissions
- create the bucket if missing
- deploy the Cloud Function

## 4) Call the function

After deployment, you can call it with a POST request.

Example using an unauthenticated URL:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "source_project": "source-bigquery-project-id",
    "dataset_id": "your_dataset",
    "table_id": "your_table",
    "destination_bucket": "your-project-id-export-bucket",
    "destination_object": "exports/your_table.csv",
    "location": "US",
    "format": "CSV"
  }' \
  "https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/export_table_to_gcs"
```

Example using query parameters instead:

```bash
curl -X GET \
  "https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/export_table_to_gcs?source_project=source-bigquery-project-id&dataset_id=your_dataset&table_id=your_table&destination_bucket=your-project-id-export-bucket&destination_object=exports/your_table.csv&location=US&format=CSV"
```

## 5) IAM / cross-project permissions

The service account used by the Cloud Function must have permissions in both projects:

- Source BigQuery project:
  - `roles/bigquery.dataViewer`
  - `roles/bigquery.jobUser`
- Destination bucket project:
  - `roles/storage.objectAdmin`

This is handled by `deploy.ps1`, but if you change the service account manually, make sure those roles are still granted.

## 6) Notes

- The function uses the default Google Cloud identity for the runtime.
- Location must match the BigQuery table location.
- CSV is the default export format, and JSON export is also supported.
- If you want this as a Cloud Run service instead of a Cloud Function, I can convert the same code to a Cloud Run service in one pass.
