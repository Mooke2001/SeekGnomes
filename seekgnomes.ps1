$ErrorActionPreference = 'Stop'
$Image = 'seekgnomes:1.3.0'
$Here = $PSScriptRoot
$Cpu = if ($env:SEEKGNOMES_CPUS) { $env:SEEKGNOMES_CPUS } else { '2' }
for ($Index = 0; $Index -lt $args.Count; $Index++) {
    if ($args[$Index] -eq '--') { break }
    if ($args[$Index] -eq '--cpus') {
        $Index++
        if ($Index -ge $args.Count) { throw '--cpus requires a positive integer.' }
        $Cpu = [string]$args[$Index]
    } elseif ([string]$args[$Index] -like '--cpus=*') {
        $Cpu = ([string]$args[$Index]).Substring(7)
    }
}
if ($Cpu -notmatch '^[1-9][0-9]*$') { throw 'SEEKGNOMES_CPUS / --cpus must be a positive integer.' }
docker info | Out-Null
if ((docker info --format '{{json .SecurityOptions}}') -match 'rootless') { throw 'This release requires rootful Docker or the Linux rootless Podman launcher.' }
$ExpectedId = if (Test-Path (Join-Path $Here 'image-id.txt')) { (Get-Content (Join-Path $Here 'image-id.txt')).Trim() -replace '^sha256:','' } else { '' }
$CurrentId = (docker image ls --no-trunc --quiet $Image) -replace '^sha256:',''
if (-not $CurrentId -or ($ExpectedId -and $CurrentId -ne $ExpectedId)) {
    $Archive = Join-Path $Here 'seekgnomes-image.tar.gz'
    if (Test-Path $Archive) {
        $Expected = ((Get-Content (Join-Path $Here 'image.sha256')) -split '\s+')[0]
        if ((Get-FileHash $Archive -Algorithm SHA256).Hash.ToLower() -ne $Expected) { throw 'Image SHA256 mismatch' }
        docker load -i $Archive
        if ($LASTEXITCODE -ne 0) { throw 'Image load failed' }
        $LoadedName = 'localhost/' + $Image
        $LoadedId = (docker image ls --no-trunc --quiet $LoadedName) -replace '^sha256:',''
        if ($LoadedId -and (-not $ExpectedId -or $LoadedId -eq $ExpectedId)) { docker tag $LoadedName $Image }
    } else { docker build -t $Image $Here }
    if ($LASTEXITCODE -ne 0) { throw 'Image load/build failed' }
}
if ($ExpectedId) {
    $CurrentId = (docker image inspect --format '{{.Id}}' $Image) -replace '^sha256:',''
    if ($CurrentId -ne $ExpectedId) { throw 'Loaded image ID does not match this release' }
}
$Results = if ($env:SEEKGNOMES_RESULTS_DIR) { $env:SEEKGNOMES_RESULTS_DIR } else { Join-Path $Here 'results' }
New-Item -ItemType Directory -Force $Results | Out-Null
$Results = (Resolve-Path $Results).Path
$Memory = if ($env:SEEKGNOMES_MEMORY) { $env:SEEKGNOMES_MEMORY } else { '16g' }
docker run --rm -it --init --read-only --cap-drop ALL --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined --pids-limit 256 --memory $Memory --cpus $Cpu --tmpfs /tmp:rw,nosuid,nodev,size=1g -v "${Results}:/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs:rw" -e SEEKGNOMES_API_KEY -e SEEKGNOMES_API_BASE_URL -e SEEKGNOMES_MODEL -e SEEKGNOMES_COMPANY -e SEEKGNOMES_HARNESS -e SEEKGNOMES_API_PROTOCOL -e SEEKGNOMES_MODEL_CONTEXT_TOKENS -e SEEKGNOMES_CONSTRUCTOR_OUTPUT_TOKENS -e SEEKGNOMES_CPUS -e SEEKGNOMES_CONSTRUCTOR_ACTIVE_SECONDS -e SEEKGNOMES_CONSTRUCTOR_INPUT_TOKENS $Image @args
exit $LASTEXITCODE
