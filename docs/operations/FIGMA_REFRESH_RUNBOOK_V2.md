# Operational Runbook: Figma Controlled Persistence Refresh V2

> **Document Version:** 2.0.0
> **Target Environment:** Development Database (`job_intelligence_dev`)
> **Target Organization:** Figma (`4916b6da-2803-4a9e-b3ce-dd41ef2a5324`)
> **Source Platform:** Greenhouse (`8525ba85-f298-4b98-bb4e-22124ccd640a`)
> **Schema Checkpoint:** `05377f2ddb14 (head)`
> **Scope:** One-time, scoped authorization refresh of 138 Figma canonical jobs and appending audit rows.

---

## 1. Safety Architecture and Operational Policies

1. **Strict Scoped Authorization Gate:**
   Mutating refresh execution against `job_intelligence_dev` is blocked by default and requires **all six** of the following conditions simultaneously:
   - CLI parameter `--allow-database-override job_intelligence_dev`
   - Active environment variable `FIGMA_REFRESH_AUTHORIZATION_TOKEN=AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14` (explicit manual confirmation phrase, not a secret credential)
   - Target database parameter specified as `job_intelligence_dev`
   - Active database connection verified as `job_intelligence_dev`
   - Target company UUID matches `4916b6da-2803-4a9e-b3ce-dd41ef2a5324`
   - Target company normalized name matches `figma`

2. **In-Transaction Atomic Rollback:**
   The refresh runs inside a single database transaction. Before commit, in-transaction pre-commit invariants assert:
   - Total canonical Figma jobs == 138
   - Status distribution: exactly 123 `REJECTED/EXCLUDED_ROLE`, 15 `REJECTED/LOCATION_NOT_ALLOWED`, 0 `REVIEW`, 0 `ELIGIBLE`
   - Tel Aviv job (`bdf7f339-da99-44e2-9b2d-fe4a7738f65d`) updated to `REJECTED/LOCATION_NOT_ALLOWED`, `normalized_location='Tel Aviv'`, `source_verified=True`, `confidence=0.50`
   - Exactly 2 `VerificationRecord` and 2 `FilterDecision` rows exist per job (1 baseline + 1 refresh)
   If any invariant check fails, the transaction is immediately aborted with `ROLLBACK`.

3. **External Artifact Storage:**
   All dumps, checksums, logs, and execution reports must be saved **outside the Git repository** in `$env:LOCALAPPDATA\AI-Job-Intelligence\backups`.

---

## 2. Phase 1 — Environment Initialization & Directory Setup

Execute in an administrative Windows PowerShell prompt:

```powershell
# Set error action preference to stop on any failure
$ErrorActionPreference = "Stop"

# Define timestamp and backup root outside Git workspace
$Timestamp = (Get-Date -Format "yyyyMMdd_HHmmss")
$BackupRoot = Join-Path $env:LOCALAPPDATA "AI-Job-Intelligence\backups"
$BackupDir = Join-Path $BackupRoot "figma_refresh_$Timestamp"

# Create directories
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
Write-Host "[OK] Backup directory initialized: $BackupDir" -ForegroundColor Green

# Define artifact paths
$DumpPath = Join-Path $BackupDir "figma_dev_$Timestamp.dump"
$SqlPath = Join-Path $BackupDir "figma_dev_$Timestamp.sql"
$ManifestPath = Join-Path $BackupDir "manifest.json"
$ReportPath = Join-Path $BackupDir "figma_refresh_result_$Timestamp.json"
```

---

## 3. Phase 2 — Pre-Execution Database Backup

Create dual-format backups (custom pg_dump format + plain-text SQL):

```powershell
Write-Host "[1/3] Generating custom-format binary backup..." -ForegroundColor Cyan
& pg_dump -h localhost -p 5432 -U postgres -Fc -d job_intelligence_dev -f "$DumpPath"
if ($LASTEXITCODE -ne 0) { throw "pg_dump binary export failed with code $LASTEXITCODE" }

Write-Host "[2/3] Generating plain-text SQL backup..." -ForegroundColor Cyan
& pg_dump -h localhost -p 5432 -U postgres -d job_intelligence_dev -f "$SqlPath"
if ($LASTEXITCODE -ne 0) { throw "pg_dump SQL export failed with code $LASTEXITCODE" }

# Verify non-empty files
$DumpItem = Get-Item "$DumpPath"
$SqlItem = Get-Item "$SqlPath"

if ($DumpItem.Length -le 0) { throw "Binary dump file is empty: $DumpPath" }
if ($SqlItem.Length -le 0) { throw "SQL dump file is empty: $SqlPath" }

Write-Host "[3/3] Generating cryptographic SHA-256 checksums..." -ForegroundColor Cyan
$DumpHash = (Get-FileHash -Path "$DumpPath" -Algorithm SHA256).Hash.ToLowerInvariant()
$SqlHash = (Get-FileHash -Path "$SqlPath" -Algorithm SHA256).Hash.ToLowerInvariant()

$Manifest = @{
    created_at = (Get-Date -AsUTC -Format "o")
    database = "job_intelligence_dev"
    files = @(
        @{
            type = "custom_dump"
            filename = $DumpItem.Name
            size_bytes = $DumpItem.Length
            sha256 = $DumpHash
        },
        @{
            type = "plain_sql"
            filename = $SqlItem.Name
            size_bytes = $SqlItem.Length
            sha256 = $SqlHash
        }
    )
}

$ManifestJson = $Manifest | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($ManifestPath, $ManifestJson, [System.Text.Encoding]::UTF8)
Write-Host "[OK] Backup complete and manifest recorded." -ForegroundColor Green
```

---

## 4. Phase 3 — Fail-Closed Checksum & Dump Integrity Verification

Programmatically re-read the manifest and verify file hashes and dump catalog:

```powershell
Write-Host "[1/2] Verifying cryptographic manifest integrity..." -ForegroundColor Cyan

if (-not (Test-Path "$ManifestPath")) { throw "Manifest file not found: $ManifestPath" }

$ManifestContent = Get-Content -Path "$ManifestPath" -Raw | ConvertFrom-Json
if ($ManifestContent.files.Count -ne 2) {
    throw "Manifest integrity failure: expected exactly 2 file entries, found $($ManifestContent.files.Count)"
}

foreach ($entry in $ManifestContent.files) {
    $targetFile = Join-Path $BackupDir $entry.filename
    if (-not (Test-Path $targetFile)) { throw "Missing backup file: $targetFile" }

    $item = Get-Item $targetFile
    if ($item.Length -ne $entry.size_bytes) {
        throw "File size mismatch for $($entry.filename): expected $($entry.size_bytes), got $($item.Length)"
    }

    $actualHash = (Get-FileHash -Path $targetFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $entry.sha256) {
        throw "SHA-256 hash mismatch for $($entry.filename): expected $($entry.sha256), got $actualHash"
    }
}
Write-Host "[OK] Checksum validation passed for all backup artifacts." -ForegroundColor Green

Write-Host "[2/2] Validating pg_restore catalog list..." -ForegroundColor Cyan
& pg_restore -l "$DumpPath" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pg_restore catalog verification failed with code $LASTEXITCODE" }
Write-Host "[OK] Binary dump catalog is valid and parseable." -ForegroundColor Green
```

---

## 5. Phase 4 — Sandbox Restoration & Data Parity Verification

Restore backup into a temporary sandbox database (`job_intelligence_dev_sandbox`) and assert 100% data parity:

```powershell
$SandboxDb = "job_intelligence_dev_sandbox"

Write-Host "[1/3] Creating sandbox database: $SandboxDb..." -ForegroundColor Cyan
& dropdb -h localhost -p 5432 -U postgres --if-exists "$SandboxDb"
& createdb -h localhost -p 5432 -U postgres "$SandboxDb"
if ($LASTEXITCODE -ne 0) { throw "Failed to create sandbox database" }

Write-Host "[2/3] Restoring dump into sandbox..." -ForegroundColor Cyan
& pg_restore -h localhost -p 5432 -U postgres -d "$SandboxDb" "$DumpPath"
if ($LASTEXITCODE -ne 0) { throw "Sandbox pg_restore failed with code $LASTEXITCODE" }

Write-Host "[3/3] Asserting full relational entity parity..." -ForegroundColor Cyan

$Query = @"
SELECT
    (SELECT COUNT(*) FROM companies WHERE id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND normalized_name = 'figma') AS company_cnt,
    (SELECT COUNT(*) FROM sources WHERE id = '8525ba85-f298-4b98-bb4e-22124ccd640a' AND source_name = 'Greenhouse') AS source_cnt,
    (SELECT COUNT(*) FROM company_sources WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS cs_cnt,
    (SELECT COUNT(*) FROM collection_runs) AS run_cnt,
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS job_cnt,
    (SELECT COUNT(*) FROM raw_jobs) AS raw_cnt,
    (SELECT COUNT(*) FROM job_source_references) AS ref_cnt,
    (SELECT COUNT(*) FROM verification_records) AS verif_cnt,
    (SELECT COUNT(*) FROM filter_decisions) AS filter_cnt,
    (SELECT COUNT(*) FROM jobs WHERE id = 'bdf7f339-da99-44e2-9b2d-fe4a7738f65d' AND eligibility_status = 'REVIEW' AND review_reason = 'POSTED_DATE_SUSPICIOUS') AS tel_aviv_cnt;
"@

$RawParity = & psql -h localhost -p 5432 -U postgres -d "$SandboxDb" -v ON_ERROR_STOP=1 -A -t -F "|" -c "$Query"
if ($LASTEXITCODE -ne 0) { throw "Parity check query failed" }

$vals = $RawParity.Trim().Split("|")
if ($vals.Count -ne 10) { throw "Unexpected parity result format: $RawParity" }

$companyCnt = [int]$vals[0]
$sourceCnt  = [int]$vals[1]
$csCnt      = [int]$vals[2]
$runCnt     = [int]$vals[3]
$jobCnt     = [int]$vals[4]
$rawCnt     = [int]$vals[5]
$refCnt     = [int]$vals[6]
$verifCnt   = [int]$vals[7]
$filterCnt  = [int]$vals[8]
$telAvivCnt = [int]$vals[9]

if ($companyCnt -ne 1)   { throw "Sandbox parity failed: expected 1 company, got $companyCnt" }
if ($sourceCnt -ne 1)    { throw "Sandbox parity failed: expected 1 source, got $sourceCnt" }
if ($csCnt -ne 1)        { throw "Sandbox parity failed: expected 1 company_source, got $csCnt" }
if ($runCnt -ne 1)       { throw "Sandbox parity failed: expected 1 collection_run, got $runCnt" }
if ($jobCnt -ne 138)     { throw "Sandbox parity failed: expected 138 jobs, got $jobCnt" }
if ($rawCnt -ne 138)     { throw "Sandbox parity failed: expected 138 raw_jobs, got $rawCnt" }
if ($refCnt -ne 138)     { throw "Sandbox parity failed: expected 138 job_source_references, got $refCnt" }
if ($verifCnt -ne 138)   { throw "Sandbox parity failed: expected 138 verification_records, got $verifCnt" }
if ($filterCnt -ne 138)  { throw "Sandbox parity failed: expected 138 filter_decisions, got $filterCnt" }
if ($telAvivCnt -ne 1)   { throw "Sandbox parity failed: Tel Aviv job initial state missing" }

Write-Host "[OK] Sandbox backup parity verified: 100% match across all 10 entity invariants." -ForegroundColor Green

# Cleanup sandbox
& dropdb -h localhost -p 5432 -U postgres "$SandboxDb"
Write-Host "[OK] Sandbox database dropped." -ForegroundColor Green
```

---

## 6. Phase 5 — Dry-Run Preview Before Apply

Execute read-only preview against `job_intelligence_dev`:

```powershell
Write-Host "Executing read-only preview..." -ForegroundColor Cyan
uv run python -m app.cli refresh `
    -c "Figma" `
    --preview `
    --target-database job_intelligence_dev

if ($LASTEXITCODE -ne 0) { throw "Preview failed with exit code $LASTEXITCODE" }
```

Expected Preview Summary:
- Total Canonical Jobs: `138`
- Jobs Requiring Update: `138`
- Jobs Already Up-to-Date: `0`
- Proposed Transitions:
  - `REJECTED -> REJECTED`: `137`
  - `REVIEW -> REJECTED`: `1` (Tel Aviv job)

---

## 7. Phase 6 — Authorized Mutation Execution

Set authorization token and execute transactional refresh:

```powershell
Write-Host "Authorizing and applying controlled persistence refresh..." -ForegroundColor Yellow

# Set explicit manual confirmation phrase (not a secret credential)
# Set manual confirmation phrase
$env:FIGMA_REFRESH_AUTHORIZATION_TOKEN = "AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14"

try {
    uv run python -m app.cli refresh `
        -c "Figma" `
        --apply `
        --target-database job_intelligence_dev `
        --allow-database-override job_intelligence_dev `
        -o "$ReportPath"

    if ($LASTEXITCODE -ne 0) {
        throw "Refresh execution failed with exit code $LASTEXITCODE"
    }
    Write-Host "[OK] Refresh applied successfully." -ForegroundColor Green
}
finally {
    # Always revoke authorization token immediately
    Remove-Item env:FIGMA_REFRESH_AUTHORIZATION_TOKEN -ErrorAction SilentlyContinue
    Write-Host "[OK] Authorization token revoked." -ForegroundColor Cyan
}
```

---

## 8. Phase 7 — Post-Refresh Database Verification

Assert expected post-refresh state directly in `job_intelligence_dev`:

```powershell
Write-Host "Running post-refresh validation queries on job_intelligence_dev..." -ForegroundColor Cyan

$VerifyQuery = @"
SELECT
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS total_jobs,
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND eligibility_status = 'REJECTED' AND rejection_reason = 'EXCLUDED_ROLE') AS excluded_role_cnt,
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND eligibility_status = 'REJECTED' AND rejection_reason = 'LOCATION_NOT_ALLOWED') AS location_cnt,
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND eligibility_status = 'REVIEW') AS review_cnt,
    (SELECT COUNT(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND eligibility_status = 'ELIGIBLE') AS eligible_cnt,
    (SELECT COUNT(*) FROM verification_records) AS total_verif_records,
    (SELECT COUNT(*) FROM filter_decisions) AS total_filter_decisions,
    (SELECT COUNT(*) FROM jobs WHERE id = 'bdf7f339-da99-44e2-9b2d-fe4a7738f65d' AND eligibility_status = 'REJECTED' AND rejection_reason = 'LOCATION_NOT_ALLOWED' AND normalized_location = 'Tel Aviv' AND source_verified = true AND verification_confidence = 0.50) AS tel_aviv_verified;
"@

$RawPost = & psql -h localhost -p 5432 -U postgres -d job_intelligence_dev -v ON_ERROR_STOP=1 -A -t -F "|" -c "$VerifyQuery"
if ($LASTEXITCODE -ne 0) { throw "Post-refresh validation query failed" }

$p = $RawPost.Trim().Split("|")
$totalJobs        = [int]$p[0]
$excludedRole     = [int]$p[1]
$locationRejected = [int]$p[2]
$reviewCnt        = [int]$p[3]
$eligibleCnt      = [int]$p[4]
$totalVerif       = [int]$p[5]
$totalFilter      = [int]$p[6]
$telAvivVerif     = [int]$p[7]

if ($totalJobs -ne 138)        { throw "Post-refresh validation failed: expected 138 jobs, found $totalJobs" }
if ($excludedRole -ne 123)     { throw "Post-refresh validation failed: expected 123 EXCLUDED_ROLE, found $excludedRole" }
if ($locationRejected -ne 15)  { throw "Post-refresh validation failed: expected 15 LOCATION_NOT_ALLOWED, found $locationRejected" }
if ($reviewCnt -ne 0)          { throw "Post-refresh validation failed: expected 0 REVIEW jobs, found $reviewCnt" }
if ($eligibleCnt -ne 0)        { throw "Post-refresh validation failed: expected 0 ELIGIBLE jobs, found $eligibleCnt" }
if ($totalVerif -ne 276)       { throw "Post-refresh validation failed: expected 276 verification records, found $totalVerif" }
if ($totalFilter -ne 276)      { throw "Post-refresh validation failed: expected 276 filter decisions, found $totalFilter" }
if ($telAvivVerif -ne 1)       { throw "Post-refresh validation failed: Tel Aviv job attributes not verified" }

Write-Host "=================================================================" -ForegroundColor Green
Write-Host "     ALL POST-REFRESH INVARIANTS VERIFIED SUCCESSFULLY           " -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
```

---

## 9. Phase 8 — Idempotency Re-Execution Check

Verify that immediately running the command again detects no changes and executes zero writes:

```powershell
Write-Host "Testing idempotency guard (should skip writes)..." -ForegroundColor Cyan

# Set manual confirmation phrase
$env:FIGMA_REFRESH_AUTHORIZATION_TOKEN = "AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14"
try {
    uv run python -m app.cli refresh `
        -c "Figma" `
        --apply `
        --target-database job_intelligence_dev `
        --allow-database-override job_intelligence_dev

    if ($LASTEXITCODE -ne 0) { throw "Idempotency check failed" }
}
finally {
    Remove-Item env:FIGMA_REFRESH_AUTHORIZATION_TOKEN -ErrorAction SilentlyContinue
}

# Verify audit counts remain exactly 276 (0 inserted)
$AuditQuery = "SELECT (SELECT COUNT(*) FROM verification_records), (SELECT COUNT(*) FROM filter_decisions);"
$AuditCounts = & psql -h localhost -p 5432 -U postgres -d job_intelligence_dev -A -t -F "|" -c "$AuditQuery"
$vCnt = [int]($AuditCounts.Trim().Split("|")[0])
$fCnt = [int]($AuditCounts.Trim().Split("|")[1])

if ($vCnt -ne 276 -or $fCnt -ne 276) {
    throw "Idempotency violated: audit counts changed to verif=$vCnt, filter=$fCnt"
}
Write-Host "[OK] Idempotency confirmed: repeated apply skipped with 0 mutations." -ForegroundColor Green
```

---

## 10. Rollback & Recovery Procedures

If any failure occurs during execution or verification, follow this two-stage recovery procedure:

### Stage 1: Restore to Isolated Recovery Database (`job_intelligence_dev_recovery`)
Do not overwrite the development database directly. First restore to an isolated recovery database to verify data integrity:

```powershell
$RecoveryDb = "job_intelligence_dev_recovery"
& dropdb -h localhost -p 5432 -U postgres --if-exists "$RecoveryDb"
& createdb -h localhost -p 5432 -U postgres "$RecoveryDb"
& pg_restore -h localhost -p 5432 -U postgres -d "$RecoveryDb" "$DumpPath"
if ($LASTEXITCODE -ne 0) { throw "Recovery DB restoration failed" }

# Verify recovery DB parity (assert 138 jobs, 138 verification records, etc.)
# Same parity query as Phase 4
```

### Stage 2: Promoted Live Restoration (Authorized Action Only)
Only after confirming recovery DB parity, restore live database if needed:

```powershell
# Terminate active connections to job_intelligence_dev
$TermQuery = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'job_intelligence_dev' AND pid <> pg_backend_pid();"
& psql -h localhost -p 5432 -U postgres -d postgres -c "$TermQuery"

# Drop and recreate live DB from backup
& dropdb -h localhost -p 5432 -U postgres job_intelligence_dev
& createdb -h localhost -p 5432 -U postgres job_intelligence_dev
& pg_restore -h localhost -p 5432 -U postgres -d job_intelligence_dev "$DumpPath"
```
