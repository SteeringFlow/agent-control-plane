# multi-agent 示例 runner（Windows PowerShell 5.1+，15.3.2）。
# 前置：ACP backend 已启动（cd backend; uvicorn app.main:app）。
# 行为：启动两个 standalone Agent（9101/9102）→ 经 API 建两条完整访问路径
# （Local Channel A/B → Connection → acp Agent A/B）→ 各发一条可区分消息 →
# Observation 断言 A≠B 且 Agent 日志零串路 → PASS/FAIL。可重复运行。
param([string]$Base = "http://127.0.0.1:8000")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$suffix = Get-Date -Format "HHmmss"

function Api([string]$Method, [string]$Path, $Body) {
    if ($null -ne $Body) {
        Invoke-RestMethod -Method $Method -Uri "$Base$Path" `
            -ContentType "application/json" -Body ($Body | ConvertTo-Json -Compress)
    } else {
        Invoke-RestMethod -Method $Method -Uri "$Base$Path"
    }
}

# 0. backend 必须已启动
try { $null = Api GET /health } catch {
    Write-Host "FAIL: ACP backend not reachable at $Base — 先启动: cd backend; uvicorn app.main:app"
    exit 1
}

# 1. 启动两个 standalone Agent（独立进程，日志落盘供串路审计）
Remove-Item "$here\agent-a.log","$here\agent-b.log" -ErrorAction SilentlyContinue
$procA = Start-Process python -ArgumentList "`"$here\agent_server.py`"","--name","AgentA","--port","9101","--log","`"$here\agent-a.log`"" `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput "$here\agent-a.out.log" -RedirectStandardError "$here\agent-a.err.log"
$procB = Start-Process python -ArgumentList "`"$here\agent_server.py`"","--name","AgentB","--port","9102","--log","`"$here\agent-b.log`"" `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput "$here\agent-b.out.log" -RedirectStandardError "$here\agent-b.err.log"

function Wait-Agent([int]$Port) {
    foreach ($i in 1..20) {
        try {
            $c = New-Object Net.Sockets.TcpClient
            $c.Connect("127.0.0.1", $Port); $c.Close(); return
        } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "agent on port $Port did not start"
}

try {
    Wait-Agent 9101; Wait-Agent 9102

    # 2. 两条完整访问路径（名字带后缀，可重复运行不撞 409）
    $user   = Api POST /api/users      @{ name = "multi-agent-$suffix" }
    $agentA = Api POST /api/agents     @{ name = "custom-agent-a-$suffix"; type = "acp" }
    $agentB = Api POST /api/agents     @{ name = "custom-agent-b-$suffix"; type = "acp" }
    $chA    = Api POST /api/channels   @{ name = "local-a-$suffix"; type = "local" }
    $chB    = Api POST /api/channels   @{ name = "local-b-$suffix"; type = "local" }
    $null   = Api POST /api/identities @{ user_id = $user.id; channel_id = $chA.id; external_id = "local-user-1" }
    $null   = Api POST /api/identities @{ user_id = $user.id; channel_id = $chB.id; external_id = "local-user-1" }
    $null   = Api POST /api/connections @{ channel_id = $chA.id; agent_id = $agentA.id; name = "path-a-$suffix";
        config = @{ protocol = "minimal"; endpoint = "http://127.0.0.1:9101" } }
    $null   = Api POST /api/connections @{ channel_id = $chB.id; agent_id = $agentB.id; name = "path-b-$suffix";
        config = @{ protocol = "minimal"; endpoint = "http://127.0.0.1:9102" } }

    # 3. 各发一条可区分消息（200 只是 ACK）
    $mA = Api POST "/api/channels/$($chA.id)/local/message" @{ external_id = "local-user-1"; text = "msg-from-A" }
    $mB = Api POST "/api/channels/$($chB.id)/local/message" @{ external_id = "local-user-1"; text = "msg-from-B" }

    # 4. 轮询 Observation 至终态
    function Wait-Observation([int]$Id) {
        foreach ($i in 1..20) {
            $obs = Api GET "/api/interactions/$Id"
            if ($obs.processing_status -in @("succeeded", "failed")) { return $obs }
            Start-Sleep 1
        }
        throw "interaction $Id did not reach terminal state"
    }
    $obsA = Wait-Observation $mA.interaction_id
    $obsB = Wait-Observation $mB.interaction_id

    # 5. 断言：A→AgentA、B→AgentB、双方零串路
    $checks = @(
        @{ desc = "A: interaction succeeded";        ok = ($obsA.processing_status -eq "succeeded" -and $obsA.delivery_status -eq "delivered") },
        @{ desc = "A: answered by AgentA";           ok = ($obsA.response_payload.content -like "AgentA*msg-from-A*") },
        @{ desc = "A: AgentA log got msg-from-A";    ok = ((Get-Content "$here\agent-a.log" -Raw) -match "msg-from-A") },
        @{ desc = "A: AgentA log clean of B";        ok = (-not ((Get-Content "$here\agent-a.log" -Raw) -match "msg-from-B")) },
        @{ desc = "B: interaction succeeded";        ok = ($obsB.processing_status -eq "succeeded" -and $obsB.delivery_status -eq "delivered") },
        @{ desc = "B: answered by AgentB";           ok = ($obsB.response_payload.content -like "AgentB*msg-from-B*") },
        @{ desc = "B: AgentB log got msg-from-B";    ok = ((Get-Content "$here\agent-b.log" -Raw) -match "msg-from-B") },
        @{ desc = "B: AgentB log clean of A";        ok = (-not ((Get-Content "$here\agent-b.log" -Raw) -match "msg-from-A")) }
    )
    $failed = 0
    foreach ($c in $checks) {
        $mark = if ($c.ok) { "PASS" } else { $FAIL = $true; $failed++; "FAIL" }
        Write-Host ("  [{0}] {1}" -f $mark, $c.desc)
    }
    Write-Host ("  A response: " + $obsA.response_payload.content)
    Write-Host ("  B response: " + $obsB.response_payload.content)
    if ($failed -eq 0) { Write-Host "MULTI-AGENT ISOLATION: PASS" } else { Write-Host "MULTI-AGENT ISOLATION: FAIL ($failed)"; exit 1 }
} finally {
    foreach ($p in @($procA, $procB)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
}
