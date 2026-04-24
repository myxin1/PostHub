param(
    [string]$ProjectName = "posthub"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "========================================================================"
    Write-Host "  $Title"
    Write-Host "========================================================================"
    Write-Host ""
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command not found: $Name"
    }
}

function Import-SimpleEnvFile {
    param(
        [string]$Path,
        [hashtable]$Target
    )
    if (-not (Test-Path $Path)) {
        return
    }
    foreach ($Line in Get-Content -Path $Path) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith("#")) {
            continue
        }
        $Separator = $Trimmed.IndexOf("=")
        if ($Separator -lt 1) {
            continue
        }
        $Name = $Trimmed.Substring(0, $Separator).Trim()
        $Value = $Trimmed.Substring($Separator + 1).Trim()
        if ($Value.Length -ge 2) {
            if (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'"))) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }
        $Target[$Name] = $Value
    }
}

function Get-DefaultValue {
    param(
        [string]$Name,
        [hashtable]$Defaults,
        [string]$Fallback = ""
    )
    $ProcessValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($ProcessValue)) {
        return $ProcessValue.Trim()
    }
    if ($Defaults.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace([string]$Defaults[$Name])) {
        return [string]$Defaults[$Name]
    }
    return $Fallback
}

function Prompt-Value {
    param(
        [string]$Label,
        [string]$DefaultValue = ""
    )
    if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
        return (Read-Host -Prompt $Label).Trim()
    }
    $Entered = Read-Host -Prompt "$Label [$DefaultValue]"
    if ([string]::IsNullOrWhiteSpace($Entered)) {
        return $DefaultValue
    }
    return $Entered.Trim()
}

function ConvertTo-PlainText {
    param([System.Security.SecureString]$SecureValue)
    if (-not $SecureValue) {
        return ""
    }
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    }
}

function Prompt-SecretValue {
    param(
        [string]$Label,
        [string]$DefaultValue = ""
    )
    while ($true) {
        if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
            $Plain = ConvertTo-PlainText (Read-Host -Prompt $Label -AsSecureString)
        }
        else {
            $Plain = ConvertTo-PlainText (Read-Host -Prompt "$Label [press enter to keep current]" -AsSecureString)
            if ([string]::IsNullOrWhiteSpace($Plain)) {
                $Plain = $DefaultValue
            }
        }
        if ($Plain.Length -ge 6) {
            return $Plain
        }
        Write-Host "Password must have at least 6 characters." -ForegroundColor Yellow
    }
}

function New-HexSecret {
    param([int]$Bytes = 32)
    $Buffer = New-Object byte[] $Bytes
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Rng.GetBytes($Buffer)
    }
    finally {
        $Rng.Dispose()
    }
    return -join ($Buffer | ForEach-Object { $_.ToString("x2") })
}

function New-Base64Secret {
    param([int]$Bytes = 32)
    $Buffer = New-Object byte[] $Bytes
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Rng.GetBytes($Buffer)
    }
    finally {
        $Rng.Dispose()
    }
    return [Convert]::ToBase64String($Buffer)
}

function Remove-VercelEnvIfPresent {
    param(
        [string]$Name,
        [string]$TargetEnv
    )
    try {
        & vercel env rm $Name $TargetEnv --yes *> $null
    }
    catch {
    }
}

function Set-VercelEnvValue {
    param(
        [string]$Name,
        [string]$Value
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return
    }
    foreach ($TargetEnv in @("production", "preview")) {
        Remove-VercelEnvIfPresent -Name $Name -TargetEnv $TargetEnv
        $Value | & vercel env add $Name $TargetEnv *> $null
    }
    Write-Host "  - $Name"
}

Write-Section "PostHUB deploy for Vercel"

Require-Command -Name "vercel"

$Defaults = @{}
Import-SimpleEnvFile -Path (Join-Path $ProjectDir "backend/.env") -Target $Defaults
Import-SimpleEnvFile -Path (Join-Path $ProjectDir ".env.local") -Target $Defaults
Import-SimpleEnvFile -Path (Join-Path $ProjectDir ".env.vercel") -Target $Defaults

try {
    $VercelUser = (& vercel whoami 2>$null).Trim()
}
catch {
    throw "You are not logged into Vercel yet. Run 'vercel login' and try again."
}

Write-Host "Vercel account: $VercelUser"

Write-Section "Project link"
$ProjectLinkFile = Join-Path $ProjectDir ".vercel/project.json"
if (Test-Path $ProjectLinkFile) {
    Write-Host "Project already linked through .vercel/project.json"
}
else {
    $LinkOutput = & vercel --yes --name $ProjectName 2>&1
    $LinkOutput | Select-Object -Last 5 | ForEach-Object { Write-Host $_ }
}

Write-Section "Runtime settings"
Write-Host "Leave DATABASE_URL empty to keep the current remote value."
Write-Host "If the project has no DATABASE_URL yet, Vercel will fall back to temporary SQLite."
Write-Host ""

$DatabaseUrl = Prompt-Value -Label "DATABASE_URL" -DefaultValue (Get-DefaultValue -Name "DATABASE_URL" -Defaults $Defaults)
$AdminLogin = Prompt-Value -Label "Admin login" -DefaultValue (Get-DefaultValue -Name "POSTHUB_ADMIN_LOGIN" -Defaults $Defaults -Fallback "adm")
$AdminEmail = Prompt-Value -Label "Admin email" -DefaultValue (Get-DefaultValue -Name "POSTHUB_ADMIN_EMAIL" -Defaults $Defaults -Fallback "admin@posthub.local")
$AdminPassword = Prompt-SecretValue -Label "Admin password" -DefaultValue (Get-DefaultValue -Name "POSTHUB_ADMIN_PASSWORD" -Defaults $Defaults)
$BaseUrl = Prompt-Value -Label "BASE_URL for Google OAuth (optional)" -DefaultValue (Get-DefaultValue -Name "BASE_URL" -Defaults $Defaults)

$JwtSecret = Get-DefaultValue -Name "JWT_SECRET" -Defaults $Defaults
if ([string]::IsNullOrWhiteSpace($JwtSecret)) { $JwtSecret = New-HexSecret }
$JwtIssuer = Get-DefaultValue -Name "JWT_ISSUER" -Defaults $Defaults -Fallback "posthub"
$JwtAudience = Get-DefaultValue -Name "JWT_AUDIENCE" -Defaults $Defaults -Fallback "posthub"
$AccessTokenTtl = Get-DefaultValue -Name "ACCESS_TOKEN_TTL_SECONDS" -Defaults $Defaults -Fallback "43200"
$SessionSecret = Get-DefaultValue -Name "SESSION_SECRET" -Defaults $Defaults
if ([string]::IsNullOrWhiteSpace($SessionSecret)) { $SessionSecret = New-HexSecret }
$EncryptionKey = Get-DefaultValue -Name "ENCRYPTION_KEY_B64" -Defaults $Defaults
if ([string]::IsNullOrWhiteSpace($EncryptionKey)) { $EncryptionKey = New-Base64Secret }
$CronSecret = Get-DefaultValue -Name "CRON_SECRET" -Defaults $Defaults
if ([string]::IsNullOrWhiteSpace($CronSecret)) { $CronSecret = New-HexSecret }
$WordpressTimeout = Get-DefaultValue -Name "WORDPRESS_TIMEOUT_SECONDS" -Defaults $Defaults -Fallback "30"
$HttpTimeout = Get-DefaultValue -Name "HTTP_TIMEOUT_SECONDS" -Defaults $Defaults -Fallback "30"
$HttpSkipVerify = Get-DefaultValue -Name "HTTP_INSECURE_SKIP_VERIFY" -Defaults $Defaults -Fallback "true"

Write-Section "Uploading environment variables"

if (-not [string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    Set-VercelEnvValue -Name "DATABASE_URL" -Value $DatabaseUrl
}

Set-VercelEnvValue -Name "JWT_SECRET" -Value $JwtSecret
Set-VercelEnvValue -Name "JWT_ISSUER" -Value $JwtIssuer
Set-VercelEnvValue -Name "JWT_AUDIENCE" -Value $JwtAudience
Set-VercelEnvValue -Name "ACCESS_TOKEN_TTL_SECONDS" -Value $AccessTokenTtl
Set-VercelEnvValue -Name "SESSION_SECRET" -Value $SessionSecret
Set-VercelEnvValue -Name "ENCRYPTION_KEY_B64" -Value $EncryptionKey
Set-VercelEnvValue -Name "POSTHUB_ADMIN_LOGIN" -Value $AdminLogin
Set-VercelEnvValue -Name "POSTHUB_ADMIN_EMAIL" -Value $AdminEmail
Set-VercelEnvValue -Name "POSTHUB_ADMIN_PASSWORD" -Value $AdminPassword
Set-VercelEnvValue -Name "CRON_SECRET" -Value $CronSecret
Set-VercelEnvValue -Name "WORDPRESS_TIMEOUT_SECONDS" -Value $WordpressTimeout
Set-VercelEnvValue -Name "HTTP_TIMEOUT_SECONDS" -Value $HttpTimeout
Set-VercelEnvValue -Name "HTTP_INSECURE_SKIP_VERIFY" -Value $HttpSkipVerify
Set-VercelEnvValue -Name "POSTHUB_INLINE_WORKER" -Value "0"

if (-not [string]::IsNullOrWhiteSpace($BaseUrl)) {
    Set-VercelEnvValue -Name "BASE_URL" -Value $BaseUrl
}

$GeminiApiKey = Get-DefaultValue -Name "GEMINI_API_KEY" -Defaults $Defaults
if (-not [string]::IsNullOrWhiteSpace($GeminiApiKey)) {
    Set-VercelEnvValue -Name "GEMINI_API_KEY" -Value $GeminiApiKey
    $GeminiModel = Get-DefaultValue -Name "GEMINI_MODEL" -Defaults $Defaults -Fallback "gemini-1.5-flash-latest"
    Set-VercelEnvValue -Name "GEMINI_MODEL" -Value $GeminiModel
}

$GoogleClientId = Get-DefaultValue -Name "GOOGLE_CLIENT_ID" -Defaults $Defaults
if (-not [string]::IsNullOrWhiteSpace($GoogleClientId)) {
    Set-VercelEnvValue -Name "GOOGLE_CLIENT_ID" -Value $GoogleClientId
    Set-VercelEnvValue -Name "GOOGLE_CLIENT_SECRET" -Value (Get-DefaultValue -Name "GOOGLE_CLIENT_SECRET" -Defaults $Defaults)
}

Write-Section "Production deploy"
$DeployOutput = & vercel --prod --yes 2>&1
$DeployOutput | Select-Object -Last 10 | ForEach-Object { Write-Host $_ }

$ProductionUrl = ""
foreach ($Line in $DeployOutput) {
    if ($Line -match '(https://[A-Za-z0-9._/-]+\.vercel\.app)') {
        $ProductionUrl = $Matches[1]
    }
}

if (-not [string]::IsNullOrWhiteSpace($ProductionUrl)) {
    Write-Section "Post-deploy setup"
    try {
        Invoke-WebRequest -Uri "$ProductionUrl/api/setup" -UseBasicParsing | Out-Null
        Write-Host "Setup endpoint completed successfully."
    }
    catch {
        Write-Warning "Could not confirm $ProductionUrl/api/setup"
    }
}

Write-Section "Done"
if ([string]::IsNullOrWhiteSpace($ProductionUrl)) {
    Write-Host "Production URL: not detected automatically"
}
else {
    Write-Host "Production URL: $ProductionUrl"
}
Write-Host "Admin login: $AdminLogin"
Write-Host "Worker mode on Vercel: 0 (cron/serverless-safe)"
Write-Host ""
Write-Host "If you use Google OAuth, make sure BASE_URL matches the public app URL."
