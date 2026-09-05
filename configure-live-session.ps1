[CmdletBinding()]
param(
    [ValidateSet("Slack", "Meta", "WhatsApp", "Email", "LinkedIn", "Shopify", "Zendesk", "HubSpot", "Smtp", "PublicUrl")]
    [string[]] $Connector = @(),
    [switch] $RunChannelCheck
)

$ErrorActionPreference = "Stop"

function Set-SessionSecret {
    param([Parameter(Mandatory)][string] $Name)

    $secureValue = Read-Host "Enter $Name (the value will not be displayed)" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plainValue)) {
            throw "$Name cannot be empty."
        }
        [Environment]::SetEnvironmentVariable($Name, $plainValue, "Process")
    }
    finally {
        if ($null -ne $plainValue) {
            $plainValue = $null
        }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Set-SessionText {
    param(
        [Parameter(Mandatory)][string] $Name,
        [string] $Default = ""
    )

    $prompt = if ($Default) { "$Name [$Default]" } else { $Name }
    $value = Read-Host $prompt
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = $Default
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$Name cannot be empty."
    }
    [Environment]::SetEnvironmentVariable($Name, $value.Trim(), "Process")
}

function Set-TenantToken {
    param([Parameter(Mandatory)][string] $SuggestedName)

    $environmentName = Read-Host "Environment variable name that holds the token [$SuggestedName]"
    if ([string]::IsNullOrWhiteSpace($environmentName)) {
        $environmentName = $SuggestedName
    }
    if ($environmentName -notmatch '^[A-Z][A-Z0-9_]{2,127}$') {
        throw "The environment variable name may contain only uppercase letters, numbers, and underscores."
    }
    Set-SessionSecret -Name $environmentName
}

if ($Connector.Count -eq 0) {
    Write-Host "Enter the integrations to configure, separated by commas." -ForegroundColor Cyan
    Write-Host "Slack, Meta, WhatsApp, Email, LinkedIn, Shopify, Zendesk, HubSpot, Smtp, PublicUrl"
    $selection = Read-Host "Integrations"
    $Connector = @($selection -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$allowed = @("Slack", "Meta", "WhatsApp", "Email", "LinkedIn", "Shopify", "Zendesk", "HubSpot", "Smtp", "PublicUrl")
foreach ($name in $Connector) {
    if ($name -notin $allowed) {
        throw "Unsupported integration: $name"
    }
}

foreach ($name in $Connector) {
    switch ($name) {
        "Slack" {
            Set-SessionSecret "FRONTDESK_SLACK_BOT_TOKEN"
            Set-SessionSecret "FRONTDESK_SLACK_SIGNING_SECRET"
            Set-SessionText "FRONTDESK_SLACK_TEAM_ID"
        }
        "Meta" {
            Set-SessionSecret "FRONTDESK_META_APP_SECRET"
            Set-SessionSecret "FRONTDESK_META_PAGE_TOKEN"
            Set-SessionSecret "FRONTDESK_META_VERIFY_TOKEN"
            Set-SessionText "FRONTDESK_META_GRAPH_VERSION" "v26.0"
        }
        "WhatsApp" {
            Set-SessionSecret "FRONTDESK_WHATSAPP_APP_SECRET"
            Set-SessionSecret "FRONTDESK_WHATSAPP_TOKEN"
            Set-SessionSecret "FRONTDESK_WHATSAPP_VERIFY_TOKEN"
            if (-not [Environment]::GetEnvironmentVariable("FRONTDESK_META_GRAPH_VERSION", "Process")) {
                Set-SessionText "FRONTDESK_META_GRAPH_VERSION" "v26.0"
            }
        }
        "Email" {
            Set-SessionSecret "FRONTDESK_EMAIL_WEBHOOK_SECRET"
        }
        "LinkedIn" {
            Set-SessionText "FRONTDESK_LINKEDIN_CLIENT_ID"
            Set-SessionSecret "FRONTDESK_LINKEDIN_CLIENT_SECRET"
            Set-SessionText "FRONTDESK_LINKEDIN_REDIRECT_URI"
            Set-SessionSecret "FRONTDESK_LINKEDIN_STATE_SECRET"
            Set-SessionText "FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS"
        }
        "Shopify" { Set-TenantToken "CUSTOMER_A_SHOPIFY_TOKEN" }
        "Zendesk" { Set-TenantToken "CUSTOMER_A_ZENDESK_TOKEN" }
        "HubSpot" { Set-TenantToken "CUSTOMER_A_HUBSPOT_TOKEN" }
        "Smtp" {
            Set-SessionText "CUSTOMER_A_SMTP_USERNAME"
            Set-SessionSecret "CUSTOMER_A_SMTP_PASSWORD"
        }
        "PublicUrl" { Set-SessionText "FRONTDESK_PUBLIC_BASE_URL" }
    }
}

Write-Host "The selected credentials are set only for this PowerShell process." -ForegroundColor Green
Write-Host "Start FrontDesk from this same window. The settings disappear when the window closes."

if ($RunChannelCheck) {
    & python (Join-Path $PSScriptRoot "verify_channels_live.py")
    exit $LASTEXITCODE
}
