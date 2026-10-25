param([Parameter(Mandatory=$true)][string]$SpecPath)

# Fixed-code adapter.  No Invoke-Expression, shell action, credentials, timers,
# elevation or restart-on-failure.  The only variable input is parsed JSON.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$sourceMarker = 'FreeTranslate durable runner v1'

function Write-Receipt($Value) {
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Depth 12 -Compress))
}

function Resolve-UserSid([string]$Value) {
    if ($Value.StartsWith('S-1-')) { return $Value }
    $account = New-Object System.Security.Principal.NTAccount($Value)
    return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
}

function Assert-Owned($Task) {
    $taskSid = Resolve-UserSid ([string]$Task.Definition.Principal.UserId)
    if ($Task.Definition.RegistrationInfo.Source -ne $sourceMarker -or
        $taskSid -ne $userSid -or
        [int]$Task.Definition.Principal.LogonType -ne 3 -or
        [int]$Task.Definition.Principal.RunLevel -ne 0) {
        throw 'Task does not belong to this adapter and current user; refusing operation.'
    }
}

function Task-Receipt($Task) {
    $definition = $Task.Definition
    $action = $definition.Actions.Item(1)
    $state = [int]$Task.State
    $stateNames = @('unknown', 'disabled', 'queued', 'ready', 'running')
    return [ordered]@{
        ok = $true; exists = $true; name = [string]$Task.Name; path = [string]$Task.Path
        current_user_sid = $userSid; current_user = $identity.Name
        state = $state; state_name = $stateNames[$state]
        last_task_result = [long]$Task.LastTaskResult
        last_run_time = $Task.LastRunTime.ToUniversalTime().ToString('o')
        principal = @{
            user_id = [string]$definition.Principal.UserId
            user_sid = (Resolve-UserSid ([string]$definition.Principal.UserId))
            logon_type = [int]$definition.Principal.LogonType
            run_level = [int]$definition.Principal.RunLevel
        }
        settings = @{
            hidden = [bool]$definition.Settings.Hidden
            execution_time_limit = [string]$definition.Settings.ExecutionTimeLimit
            multiple_instances = [int]$definition.Settings.MultipleInstances
            stop_if_going_on_batteries = [bool]$definition.Settings.StopIfGoingOnBatteries
            disallow_start_if_on_batteries = [bool]$definition.Settings.DisallowStartIfOnBatteries
            run_only_if_idle = [bool]$definition.Settings.RunOnlyIfIdle
            run_only_if_network_available = [bool]$definition.Settings.RunOnlyIfNetworkAvailable
            restart_count = [int]$definition.Settings.RestartCount
            trigger_count = [int]$definition.Triggers.Count
        }
        action = @{
            executable = [string]$action.Path; arguments = [string]$action.Arguments
            cwd = [string]$action.WorkingDirectory
        }
    }
}

try {
    $request = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $userSid = $identity.User.Value
    if ($request.operation -eq 'identity') {
        Write-Receipt @{ok=$true; current_user_sid=$userSid; current_user=$identity.Name}
        exit 0
    }
    $name = [string]$request.name
    if ($name -cnotmatch '^FreeTranslate-([0-9a-f]{32}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12})$') {
        throw 'Invalid task name; expected FreeTranslate- plus lowercase UUID.'
    }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')

    if ($request.operation -eq 'register') {
        $executable = [string]$request.executable
        $workingDirectory = [string]$request.cwd
        if (-not [System.IO.Path]::IsPathRooted($executable) -or
            -not (Test-Path -LiteralPath $executable -PathType Leaf) -or
            -not [System.IO.Path]::IsPathRooted($workingDirectory) -or
            -not (Test-Path -LiteralPath $workingDirectory -PathType Container)) {
            throw 'Action executable and working directory must exist at absolute paths.'
        }
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Source = $sourceMarker
        $definition.RegistrationInfo.Author = $identity.Name
        $definition.RegistrationInfo.Description = 'User-requested on-demand local translation worker; no automatic retries.'
        $definition.Principal.UserId = $userSid
        $definition.Principal.LogonType = 3 # InteractiveToken, no password
        $definition.Principal.RunLevel = 0 # LUA/Limited, no elevation
        $definition.Settings.Enabled = $true
        $definition.Settings.Hidden = $true
        $definition.Settings.AllowDemandStart = $true
        $definition.Settings.ExecutionTimeLimit = 'PT0S'
        $definition.Settings.MultipleInstances = 2 # IgnoreNew
        $definition.Settings.StopIfGoingOnBatteries = $false
        $definition.Settings.DisallowStartIfOnBatteries = $false
        $definition.Settings.RunOnlyIfIdle = $false
        $definition.Settings.RunOnlyIfNetworkAvailable = $false
        $definition.Settings.StartWhenAvailable = $false
        $definition.Settings.WakeToRun = $false
        $definition.Settings.RestartCount = 0
        $action = $definition.Actions.Create(0) # ExecAction, executable direct
        $action.Path = $executable
        $action.Arguments = [string]$request.arguments
        $action.WorkingDirectory = $workingDirectory
        $task = $folder.RegisterTaskDefinition($name, $definition, 2, $userSid, $null, 3, $null)
        Assert-Owned $task
        Write-Receipt (Task-Receipt $task)
        exit 0
    }

    $task = $null
    try { $task = $folder.GetTask($name) }
    catch {
        # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND), not permission/service errors.
        if ($_.Exception.HResult -ne -2147024894) { throw }
    }
    if ($null -eq $task) {
        if ($request.operation -in @('status', 'delete')) {
            Write-Receipt @{ok=$true; exists=$false; deleted=$false; name=$name; current_user_sid=$userSid}
            exit 0
        }
        throw 'Task does not exist.'
    }
    Assert-Owned $task
    switch ($request.operation) {
        'status' { Write-Receipt (Task-Receipt $task) }
        'run' {
            $instance = $task.Run($null)
            $receipt = Task-Receipt $task
            $receipt['instance_guid'] = [string]$instance.InstanceGuid
            $receipt['engine_pid'] = [int]$instance.EnginePID
            Write-Receipt $receipt
        }
        'delete' {
            $folder.DeleteTask($name, 0)
            Write-Receipt @{ok=$true; exists=$false; deleted=$true; name=$name; current_user_sid=$userSid}
        }
        default { throw 'Unsupported scheduler operation.' }
    }
    exit 0
} catch {
    Write-Receipt @{ok=$false; error=$_.Exception.Message; hresult=$_.Exception.HResult}
    exit 1
}
