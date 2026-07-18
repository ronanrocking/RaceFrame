$ErrorActionPreference = "Stop"

$EventId = "97f47890-d71b-4b8c-be3d-9b0fd07ea364"
$BaseUrl = "https://raceframe.ronanrocking.com"
$DatasetDir = "D:\PWD\Porjects\raceframe\downloads\udupi-triathlon-2025-5865-full"
$OutDir = Join-Path $DatasetDir "evaluation"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$participantDirs = Get-ChildItem -Path $DatasetDir -Directory |
    Where-Object { $_.Name -ne "common" -and $_.Name -ne "evaluation" -and $_.Name -match "^\d+$" } |
    Sort-Object { [int]$_.Name }

$summaryRows = @()
$diffRows = @()

function Get-ImageUuidFromName {
    param([string]$Name)
    $match = [regex]::Match($Name, "[0-9a-f-]{36}(?=\.jpg$)")
    if ($match.Success) {
        return $match.Value
    }
    return $null
}

foreach ($dir in $participantDirs) {
    $bib = $dir.Name
    $expectedFiles = @(Get-ChildItem -Path $dir.FullName -File -Filter "*.jpg" | ForEach-Object { $_.Name } | Sort-Object -Unique)

    Write-Host "searching bib=$bib expected=$($expectedFiles.Count)"

    $actualFiles = @()
    $errorMessage = ""
    try {
        $body = "participant_query=$([uri]::EscapeDataString($bib))"
        $response = Invoke-WebRequest `
            -Uri "$BaseUrl/user/events/$EventId/bib-only" `
            -Method Post `
            -Body $body `
            -ContentType "application/x-www-form-urlencoded" `
            -UseBasicParsing `
            -MaximumRedirection 5

        $actualFiles = @(
            [regex]::Matches($response.Content, "[0-9]{4}_[0-9]{3}_[0-9a-f-]{36}\.jpg") |
                ForEach-Object { $_.Value } |
                Sort-Object -Unique
        )
    } catch {
        $errorMessage = $_.Exception.Message
        Write-Host "error bib=$bib $errorMessage"
    }

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

    if ($missing.Count -gt 0 -or $unexpected.Count -gt 0 -or $errorMessage) {
        $diffRows += [pscustomobject]@{
            bib_number = $bib
            images_found_in_software = $actualByUuid.Count
            images_expected_downloaded_dataset = $expectedByUuid.Count
            percent_match = $matchPercent
            missing_expected_images = $missing
            unexpected_software_images = $unexpected
            error = $errorMessage
        }
    }

    Start-Sleep -Milliseconds 250
}

$summaryPath = Join-Path $OutDir "bib_only_reinforcement_summary.csv"
$diffPath = Join-Path $OutDir "bib_only_reinforcement_diffs.json"

$summaryRows | Export-Csv -NoTypeInformation -Encoding UTF8 $summaryPath
$diffRows | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $diffPath

[pscustomobject]@{
    summary_csv = $summaryPath
    diffs_json = $diffPath
    participants_evaluated = $summaryRows.Count
    faulting_bibs = $diffRows.Count
    exact_matches = @($summaryRows | Where-Object { [double]$_.percent_match -eq 100 }).Count
} | Format-List
