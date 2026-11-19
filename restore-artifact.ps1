param([Parameter(Mandatory = $true)][string]$Manifest)
$ErrorActionPreference = 'Stop'
$ManifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$Directory = Split-Path -Parent $ManifestPath
$Record = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
if ($Record.schema -ne 'seekgnomes-multipart/v1') { throw 'Unknown multipart release format.' }
$Name = [string]$Record.name
if ([IO.Path]::GetFileName($Name) -ne $Name -or $Name -in @('', '.', '..')) { throw 'Unsafe archive filename.' }
$Destination = Join-Path $Directory $Name
$Temporary = $Destination + '.restore-partial'
if ((Test-Path -LiteralPath $Destination) -or (Test-Path -LiteralPath $Temporary)) { throw 'Restore destination already exists.' }
$Output = $null
try {
    $Output = [IO.File]::Open($Temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $Index = 0
    $Total = [long]0
    foreach ($Part in $Record.parts) {
        $Index++
        $ExpectedName = $Name + ('.part{0:D3}' -f $Index)
        if ([string]$Part.name -ne $ExpectedName) { throw 'Unexpected multipart filename.' }
        $PartPath = Join-Path $Directory $ExpectedName
        $Info = Get-Item -LiteralPath $PartPath
        if ($Info.LinkType -or $Info.PSIsContainer -or $Info.Length -ne [long]$Part.bytes) { throw 'Invalid multipart size or link.' }
        if ((Get-FileHash -LiteralPath $PartPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$Part.sha256) { throw 'Multipart SHA256 mismatch.' }
        $PartStream = [IO.File]::OpenRead($PartPath)
        try { $PartStream.CopyTo($Output) } finally { $PartStream.Dispose() }
        $Total += $Info.Length
    }
    $Output.Dispose()
    $Output = $null
    if ($Index -eq 0 -or $Total -ne [long]$Record.bytes) { throw 'Complete archive size mismatch.' }
    if ((Get-FileHash -LiteralPath $Temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$Record.sha256) { throw 'Complete archive SHA256 mismatch.' }
    Move-Item -LiteralPath $Temporary -Destination $Destination
    Write-Output $Destination
} catch {
    if ($null -ne $Output) { $Output.Dispose() }
    if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
    throw
}
