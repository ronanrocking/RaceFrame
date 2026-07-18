param(
    [string]$ActualFileName = "live_bib_only_actual.json",
    [string]$SummaryFileName = "bib_only_reinforcement_summary.csv",
    [string]$DiffFileName = "bib_only_reinforcement_diffs.json",
    [string]$EvaluationSubdir = ""
)

$ErrorActionPreference = "Stop"

$DatasetDir = "D:\PWD\Porjects\raceframe\downloads\udupi-triathlon-2025-5865-full"
$EvalDir = Join-Path $DatasetDir "evaluation"
if ($EvaluationSubdir) {
    $EvalDir = Join-Path $EvalDir $EvaluationSubdir
}
New-Item -ItemType Directory -Force -Path $EvalDir | Out-Null
$ActualPath = Join-Path $EvalDir $ActualFileName

function Get-ImageUuidFromName {
    param([string]$Name)
    $match = [regex]::Match($Name, "[0-9a-f-]{36}(?=\.jpg$)")
    if ($match.Success) {
        return $match.Value
    }
    return $null
}

$actualJson = Get-Content -Raw $ActualPath | ConvertFrom-Json
$actualRows = @($actualJson)
$summaryRows = @()
$diffRows = @()

foreach ($row in $actualRows) {
    $bib = [string]$row.bib_number
    $dir = Join-Path $DatasetDir $bib
    if (-not (Test-Path $dir)) {
        continue
    }

    $expectedFiles = @(Get-ChildItem -Path $dir -File -Filter "*.jpg" | ForEach-Object { $_.Name } | Sort-Object -Unique)
    $actualFiles = @($row.images | Sort-Object -Unique)

    $expectedByUuid = @{}
    foreach ($name in $expectedFiles) {
        $uuid = Get-ImageUuidFromName $name
        if ($uuid) { $expectedByUuid[$uuid] = $name }
    }

    $actualByUuid = @{}
    foreach ($name in $actualFiles) {
        $uuid = Get-ImageUuidFromName $name
        if ($uuid) { $actualByUuid[$uuid] = $name }
    }

    $expectedUuids = @($expectedByUuid.Keys)
    $actualUuids = @($actualByUuid.Keys)
    $intersection = @($expectedUuids | Where-Object { $actualByUuid.ContainsKey($_) })
    $missing = @($expectedUuids | Where-Object { -not $actualByUuid.ContainsKey($_) } | ForEach-Object { $expectedByUuid[$_] } | Sort-Object)
    $unexpected = @($actualUuids | Where-Object { -not $expectedByUuid.ContainsKey($_) } | ForEach-Object { $actualByUuid[$_] } | Sort-Object)
    $unionCount = @($expectedUuids + $actualUuids | Sort-Object -Unique).Count
    $matchPercent = if ($unionCount -gt 0) { [math]::Round(($intersection.Count / $unionCount) * 100, 2) } else { 100 }

    $summaryRows += [pscustomobject]@{
        bib_number = $bib
        images_found_in_software = $actualByUuid.Count
        images_expected_downloaded_dataset = $expectedByUuid.Count
        percent_match = $matchPercent
    }

    if ($missing.Count -gt 0 -or $unexpected.Count -gt 0 -or $row.error) {
        $diffRows += [pscustomobject]@{
            bib_number = $bib
            seed_count = $row.seed_count
            reinforced_count = $row.reinforced_count
            images_found_in_software = $actualByUuid.Count
            images_expected_downloaded_dataset = $expectedByUuid.Count
            percent_match = $matchPercent
            missing_expected_images = $missing
            unexpected_software_images = $unexpected
            error = $row.error
        }
    }
}

$summaryPath = Join-Path $EvalDir $SummaryFileName
$diffPath = Join-Path $EvalDir $DiffFileName

$summaryRows | Sort-Object { [int]$_.bib_number } | Export-Csv -NoTypeInformation -Encoding UTF8 $summaryPath
$diffRows | Sort-Object { [int]$_.bib_number } | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $diffPath

[pscustomobject]@{
    summary_csv = $summaryPath
    diffs_json = $diffPath
    participants_evaluated = $summaryRows.Count
    faulting_bibs = $diffRows.Count
    exact_matches = @($summaryRows | Where-Object { [double]$_.percent_match -eq 100 }).Count
    average_percent_match = [math]::Round((($summaryRows | Measure-Object -Property percent_match -Average).Average), 2)
} | Format-List
