$ErrorActionPreference = 'Stop'

# Update these values before running the script.
$ProjectId = if ($env:PROJECT_ID) { $env:PROJECT_ID } else { 'jag-data-hub-01' }
$Region = if ($env:REGION) { $env:REGION } else { 'us-central1' }
$FunctionName = if ($env:FUNCTION_NAME) { $env:FUNCTION_NAME } else { 'export_table_to_gcs' }
$SourceProject = if ($env:SOURCE_PROJECT) { $env:SOURCE_PROJECT } else { 'your-bigquery-project-id' }
$DatasetId = if ($env:DATASET_ID) { $env:DATASET_ID } else { 'your_dataset_id' }
$TableId = if ($env:TABLE_ID) { $env:TABLE_ID } else { 'your_table_id' }
$DestBucket = if ($env:DEST_BUCKET) { $env:DEST_BUCKET } else { "$ProjectId-export-bucket" }
$DestObject = if ($env:DEST_OBJECT) { $env:DEST_OBJECT } else { "exports/$TableId.csv" }
$Location = if ($env:LOCATION) { $env:LOCATION } else { 'US' }
$Format = if ($env:FORMAT) { $env:FORMAT } else { 'CSV' }
$AllowUnauthenticated = if ($env:ALLOW_UNAUTHENTICATED) { $env:ALLOW_UNAUTHENTICATED } else { 'false' }
$ServiceAccount = if ($env:SERVICE_ACCOUNT) { $env:SERVICE_ACCOUNT } else { "$ProjectId@appspot.gserviceaccount.com" }

Write-Host 'Enabling required Google Cloud APIs...'
gcloud services enable `
  cloudfunctions.googleapis.com `
  cloudbuild.googleapis.com `
  bigquery.googleapis.com `
  storage.googleapis.com `
  --project="$ProjectId"

Write-Host 'Ensuring runtime service account exists...'
$saName = $ServiceAccount.Split('@')[0]
$saExists = gcloud iam service-accounts describe $ServiceAccount --project="$ProjectId" 2>$null
if (-not $saExists) {
  gcloud iam service-accounts create $saName --project="$ProjectId" --description="Cloud Function service account for BigQuery exports"
}

Write-Host 'Granting BigQuery permissions to the runtime service account...'
gcloud projects add-iam-policy-binding $SourceProject --member="serviceAccount:$ServiceAccount" --role="roles/bigquery.dataViewer" | Out-Null
gcloud projects add-iam-policy-binding $SourceProject --member="serviceAccount:$ServiceAccount" --role="roles/bigquery.jobUser" | Out-Null

Write-Host 'Granting Storage permissions to the runtime service account...'
gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$ServiceAccount" --role="roles/storage.objectAdmin" | Out-Null

Write-Host 'Creating destination bucket if it does not exist...'
$bucketExists = gsutil ls "gs://$DestBucket" 2>$null
if (-not $bucketExists) {
  gsutil mb -p $ProjectId -l $Region "gs://$DestBucket"
}

$cmdArgs = @(
  'functions', 'deploy', $FunctionName,
  '--project', $ProjectId,
  '--region', $Region,
  '--runtime=python314',
  '--trigger-http',
  '--entry-point=export_table_to_gcs',
  '--service-account', $ServiceAccount,
  '--source=.',
  "--set-env-vars=SOURCE_PROJECT=$SourceProject,DATASET_ID=$DatasetId,TABLE_ID=$TableId,DESTINATION_BUCKET=$DestBucket,DESTINATION_OBJECT=$DestObject,LOCATION=$Location,FORMAT=$Format"
)

if ($AllowUnauthenticated.ToLower() -eq 'true') {
  $cmdArgs += '--allow-unauthenticated'
}

Write-Host "Deploying Cloud Function $FunctionName..."
gcloud @cmdArgs

Write-Host ""
Write-Host "Deployment complete."
Write-Host "Function URL: https://$Region-$ProjectId.cloudfunctions.net/$FunctionName"
