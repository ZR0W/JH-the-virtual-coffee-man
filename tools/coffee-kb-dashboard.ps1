<#
.SYNOPSIS
  Show the coffee-kb dashboard from the home server in your browser (run on the Windows PC).

.DESCRIPTION
  Over your existing SSH key: regenerates data/dashboard.html on the server (offline, reads
  only local files), copies it to %TEMP%, and opens it. No web server, no open port, nothing
  leaves your network.

.EXAMPLE
  .\tools\coffee-kb-dashboard.ps1 -Server myserver
  # myserver = an SSH host alias from ~/.ssh/config, or user@host
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$RepoDir = "coffee-kb",              # path on the server, relative to your home
    [string]$Uv = "~/.local/bin/uv"
)
$ErrorActionPreference = "Stop"
$local = Join-Path $env:TEMP "coffee-kb-dashboard.html"

ssh $Server "cd $RepoDir && $Uv run --quiet coffee-kb dashboard --output data/dashboard.html"
if ($LASTEXITCODE -ne 0) { throw "generating the dashboard on $Server failed (exit $LASTEXITCODE)" }
scp -q "${Server}:$RepoDir/data/dashboard.html" $local
if ($LASTEXITCODE -ne 0) { throw "copying the dashboard from $Server failed (exit $LASTEXITCODE)" }
Start-Process $local
