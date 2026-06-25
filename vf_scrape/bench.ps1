# VF Scrape — bench unifié (un seul script Python)
# Usage:
#   .\vf_scrape\bench.ps1
#   .\vf_scrape\bench.ps1 -Query "video projecteur" -Category projector -PriceMin 50 -PriceMax 900
#   .\vf_scrape\bench.ps1 -Platform vinted
#   .\vf_scrape\bench.ps1 -Headed

param(
    [string]$Query = "New Nintendo 3DS XL",
    [string]$Category = "console_ds",
    [int]$PriceMin = 0,
    [int]$PriceMax = 0,
    [string]$Platform = "all",
    [int]$Max = 0,
    [switch]$Headed,
    [switch]$Verbose,
    [switch]$TestScrapers,
    [string]$LogLevel = "DEBUG",
    [string]$OutDir = "bench_out"
)

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$BenchFlag = if ($TestScrapers) { "--test-scrapers" } else { "--bench-all" }
$Args = @(
    "-m", "vf_scrape",
    $BenchFlag,
    "-q", $Query,
    "--category", $Category,
    "--out-dir", $OutDir
)
if ($TestScrapers) { $Args += @("--log-level", $LogLevel) }

if ($PriceMin -gt 0) { $Args += @("--price-min", "$PriceMin") }
if ($PriceMax -gt 0) { $Args += @("--price-max", "$PriceMax") }
if ($Platform -ne "all") { $Args += @("-p", $Platform) }
if ($Max -gt 0) { $Args += @("--max", "$Max") }
if ($Headed) { $Args += "--headed" }
if ($Verbose) { $Args += "-v" }

Write-Host ">> py $($Args -join ' ')" -ForegroundColor DarkGray
py @Args
