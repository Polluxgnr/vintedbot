# VF Scrape - full-log scraper test runner
# Usage:
#   .\scripts\test_scrapers.ps1
#   .\scripts\test_scrapers.ps1 -Query "video projecteur" -Category projector -PriceMin 50 -PriceMax 900
#   .\scripts\test_scrapers.ps1 -Platform vinted,wallapop -Verbose

param(
    [string]$Query = "video projecteur",
    [string]$Category = "projector",
    [int]$PriceMin = 50,
    [int]$PriceMax = 900,
    [string]$Platform = "all",
    [int]$Max = 0,
    [switch]$Headed,
    [switch]$Verbose,
    [string]$LogLevel = "DEBUG",
    [string]$OutDir = "bench_out"
)

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$PyArgs = @(
    "-m", "vf_scrape",
    "--test-scrapers",
    "-q", $Query,
    "--category", $Category,
    "--out-dir", $OutDir,
    "--log-level", $LogLevel
)

if ($PriceMin -gt 0) { $PyArgs += @("--price-min", "$PriceMin") }
if ($PriceMax -gt 0) { $PyArgs += @("--price-max", "$PriceMax") }
if ($Platform -ne "all") { $PyArgs += @("-p", $Platform) }
if ($Max -gt 0) { $PyArgs += @("--max", "$Max") }
if ($Headed) { $PyArgs += "--headed" }
if ($Verbose) { $PyArgs += "-v" }

Write-Host ">> py $($PyArgs -join ' ')" -ForegroundColor DarkGray
py @PyArgs
