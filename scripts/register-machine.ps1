<#
register-machine.ps1 — прописать ТЕКУЩУЮ машину в config/machines.yaml.

Запускать НА той машине, которую регистрируешь, из корня репо Content_factory.
Скрипт сам читает MachineGuid + hostname и заполняет блок с указанным -Name
(machine_guid, hostname, role, runs_bot). command_key не трогает — он задан в yaml.

Пример (на ноуте):
    pwsh scripts/register-machine.ps1 -Name laptop -Role card-agent
    git add config/machines.yaml
    git commit -m "chore(ops): реестр машин — laptop"
    git push origin master

Флаги:
    -Name      имя блока в machines.yaml (desktop|laptop)     [обязательно]
    -Role      значение role                                  [по умолчанию пусто]
    -RunsBot   выставить runs_bot: true (иначе false)
#>
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [string]$Role = "",
    [switch]$RunsBot
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$yamlPath = Join-Path $repo "config/machines.yaml"
if (-not (Test-Path $yamlPath)) {
    throw "Не найден $yamlPath — запусти из корня репо Content_factory."
}

$guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
$hn = $env:COMPUTERNAME
$runsBotStr = if ($RunsBot.IsPresent) { "true" } else { "false" }

$lines = Get-Content -LiteralPath $yamlPath
$out = [System.Collections.Generic.List[string]]::new()
$inBlock = $false
$found = $false

foreach ($line in $lines) {
    if ($line -match '^\s*-\s+name:\s*(\S+)') {
        $inBlock = ($Matches[1] -eq $Name)
        if ($inBlock) { $found = $true }
        $out.Add($line); continue
    }
    if ($inBlock -and $line -match '^(\s*)machine_guid:') { $out.Add(("{0}machine_guid: `"{1}`"" -f $Matches[1], $guid)); continue }
    if ($inBlock -and $line -match '^(\s*)hostname:')     { $out.Add(("{0}hostname: `"{1}`"" -f $Matches[1], $hn)); continue }
    if ($inBlock -and $line -match '^(\s*)role:')         { $out.Add(("{0}role: `"{1}`"" -f $Matches[1], $Role)); continue }
    if ($inBlock -and $line -match '^(\s*)runs_bot:')     { $out.Add(("{0}runs_bot: {1}" -f $Matches[1], $runsBotStr)); continue }
    $out.Add($line)
}

if (-not $found) { throw "В machines.yaml нет блока name: $Name (есть: desktop, laptop)" }

Set-Content -LiteralPath $yamlPath -Value $out -Encoding utf8
Write-Host "OK: $Name -> guid=$guid host=$hn role='$Role' runs_bot=$runsBotStr"
Write-Host "Дальше: git add config/machines.yaml; git commit -m 'chore(ops): реестр машин - $Name'; git push origin master"
