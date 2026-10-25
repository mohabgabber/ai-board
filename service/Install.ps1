#requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Owner,
    [Parameter(Mandatory)][ValidatePattern('^S-1-\d+(-\d+)+$')][string]$OwnerSid,
    [string]$Binary = (Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\ai-board.exe')
)
$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Source).Path
$Binary = (Resolve-Path -LiteralPath $Binary).Path
$account = New-Object System.Security.Principal.NTAccount($Owner)
$resolvedSid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
if ($resolvedSid -ne $OwnerSid) { throw 'Owner and OwnerSid must identify the same actual account.' }
& $Binary check --root $Source
if ($LASTEXITCODE) { throw 'Source board did not validate; nothing installed.' }
$serviceName = 'AIBoard'
$app = Join-Path $env:ProgramFiles 'AIBoard'
$data = Join-Path $env:ProgramData 'AIBoard'
$site = Join-Path $data 'site'
$installedBinary = Join-Path $app 'ai-board.exe'
$configPath = Join-Path $app 'config.json'
$taskName = 'AIBoard-Publish'
$existing = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
if ($existing -and $existing.PathName -notlike ('"' + $installedBinary + '"*')) {
    throw 'An unrelated service already uses the AIBoard name.'
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and @($task.Actions | Where-Object { $_.Execute -ne $installedBinary }).Count) {
    throw 'An unrelated scheduled task already uses the AIBoard-Publish name.'
}
if ($task) { Stop-ScheduledTask -TaskName $taskName }
if ($existing) { Stop-Service -Name $serviceName }
New-Item -ItemType Directory -Path $app,$data,$site -Force | Out-Null
& icacls.exe $app /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE) { throw 'Cannot secure the executable directory.' }
& icacls.exe $data /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE) { throw 'Cannot secure the cache directory.' }
& icacls.exe $site /grant:r ('*' + $OwnerSid + ':(OI)(CI)M') | Out-Null
if ($LASTEXITCODE) { throw 'Cannot grant the publisher access to its cache.' }
Copy-Item -LiteralPath $Binary -Destination $installedBinary -Force
& $installedBinary sync --source $Source --root $site
if ($LASTEXITCODE) { throw 'Initial cache publication failed.' }
@{root=$site;listen='127.0.0.1:8080';redirect='';hostname='localhost'} |
    ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
$command = '"' + $installedBinary + '" service --config "' + $configPath + '"'
if (-not $existing) {
    New-Service -Name $serviceName -BinaryPathName $command -DisplayName 'AI Board' -StartupType Automatic | Out-Null
}
$serviceObject = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
$change = Invoke-CimMethod -InputObject $serviceObject -MethodName Change -Arguments @{
    PathName=$command;StartMode='Automatic';StartName='NT AUTHORITY\LocalService';StartPassword=''
}
if ($change.ReturnValue -ne 0) { throw "Service configuration failed: $($change.ReturnValue)" }
& sc.exe failure $serviceName reset= 86400 actions= restart/5000/restart/15000/restart/30000 | Out-Null
if ($LASTEXITCODE) { throw 'Service recovery configuration failed.' }
$arguments = 'sync --source "' + $Source + '" --root "' + $site + '" --watch'
$action = New-ScheduledTaskAction -Execute $installedBinary -Argument $arguments -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Owner
$principal = New-ScheduledTaskPrincipal -UserId $Owner -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-Service -Name $serviceName
Start-ScheduledTask -TaskName $taskName
Write-Output 'AI Board installed on loopback: http://127.0.0.1:8080/. No firewall, DNS, hosts or power settings changed.'
