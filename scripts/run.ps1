<#
.SYNOPSIS
    一键脚本：把 README「快速开始」里那段手敲命令固化下来。

.DESCRIPTION
    这个仓库的 README 有一整节手敲命令（建 venv、装依赖、跑用例、跑压测、起看板），
    每条都有"必须用 .venv 里的 python"、"必须先设 PYTHONIOENCODING"这类隐含前提。
    手敲第三次就会漏掉其中一条 —— 而漏掉 PYTHONIOENCODING 的表现是
    "中文报告打崩"，不是"命令报错"，于是人往往以为是平台 bug。

    所以把它固化成脚本：前提写在脚本里，人只需要说要做什么。

    **本脚本只做编排，不实现任何判定逻辑。** 退出码一律透传自 platform_cli.py
    （0 放行 / 1 拦住），因为门禁的结论只能由平台给出 —— 在脚本里
    再判一次就会出现"脚本绿、门禁红"这种最难查的分歧。

    编码注意事项（**不要"顺手修好"**）：本文件必须带 UTF-8 BOM。
    Windows PowerShell 5.1 读取**无 BOM** 的 .ps1 时会按系统 ANSI 代码页
    （中文 Windows 上是 GBK）解码，于是本文件里的中文注释被解成乱码字节，
    解析在参数块处直接崩：
        Unexpected token '}' in expression or statement.
    实测：同一份内容带 BOM → 退出码 0；去掉 BOM → 退出码 1 且报上面这行。
    PowerShell 7 默认按 UTF-8 读，不要求 BOM；为兼容系统自带的 5.1，保留 BOM。

.PARAMETER Task
    help     打印将要执行的命令，不真跑（默认）—— 也是验收标准里"跑到打印级别"的那条
    setup    建 venv 并装依赖（运行时 + 开发工具链）
    env      显示当前环境档案与生效变量
    test     跑平台自身用例（379 passed / 19 skipped，约 130 秒）
    cov      跑用例并套覆盖率门槛（口径：只算业务代码）
    lint     ruff + mypy + 依赖清单一致性
    lock     重新生成 requirements.lock
    perf     跑一轮真压测 + 门禁（约 1 分钟，20k 数据 / 10u+20u）
    probe    N+1 缺陷探针（**期望它以退出码 1 结束**，那是门禁在正常工作）
    dash     起看板（前台，Ctrl+C 退出）
    all      setup → lint → test

.PARAMETER Profile
    环境档案：dev / staging / prod / off（off = 不套档案）。
    不传这个参数时**不设 PERF_ENV**，行为与不用本脚本逐字节一致。

.PARAMETER Items
    被测数据量，默认 20000（与 README §1 表里 7.81× 那组数字同量级）。

.PARAMETER Users
    压测并发梯度，默认 10 20。

.PARAMETER Duration
    每级持续秒数，默认 8。

.EXAMPLE
    # Windows PowerShell 5.1（系统自带；脚本未签名，需 Bypass）
    powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Task test
.EXAMPLE
    # PowerShell 7+（若已安装）
    pwsh -File scripts\run.ps1 -Task perf -Profile dev -Items 20000 -Users 10 20 40
#>
[CmdletBinding()]
param(
    [ValidateSet('help', 'setup', 'env', 'test', 'cov', 'lint', 'lock', 'perf', 'probe', 'dash', 'all')]
    [string]$Task = 'help',

    [ValidateSet('', 'off', 'dev', 'staging', 'prod')]
    [string]$Profile = '',

    [int]$Items = 20000,
    [int[]]$Users = @(10, 20),
    [double]$Duration = 8
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot 在 -File 调用下是本脚本所在目录（scripts/），仓库根是它的父目录。
# 不用 $(Get-Location)：那会随人所在的目录变，脚本就不再可复现了。
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'

# ---- Windows 控制台编码：不是可选项 ----
# 默认 GBK，中文报告会直接抛 UnicodeEncodeError。
$env:PYTHONIOENCODING = 'utf-8'
if ($Host.Name -eq 'ConsoleHost') {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
}

# 只有在显式传了 -Profile 时才设置 PERF_ENV。
# 这一条是刻意的：脚本的默认行为必须与"不用脚本、直接敲命令"完全一致，
# 否则"我本地是绿的"和"CI 是绿的"之间就多了一个看不见的变量。
if ($Profile -ne '') {
    $env:PERF_ENV = $Profile
}

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Assert-Venv {
    if (-not (Test-Path $Python)) {
        Write-Host "找不到虚拟环境：$Python" -ForegroundColor Red
        Write-Host "先跑：powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Task setup" -ForegroundColor Yellow
        exit 2
    }
}

# 每个任务都返回"将要执行的命令 + 参数"，-Task help 把它打印出来，
# 其余任务用 & 真正调用。
#
# 诚实边界：`Get-Commands`（本函数）与 `Invoke-Task` 其实是**两份定义** ——
# 一处是给人看的字符串，一处是真正的调用。原先这里写「help 的输出不可能
# 与实际执行的命令漂移」，那是假的：两处必须人工保持一致。
# `run.sh:43-50` 已如实承认同一件事，这里与它对齐。
# 为什么不做成"一份定义两处用"：PowerShell 里要把「命令 + 参数」当数据存，
# 既要不经 Invoke-Expression（引号地狱）、又要能原样显示，代价远大于
# 这两份几十行的清单。真要做，就得改成"数据 + 解释器"，那是另一个设计。
function Get-Commands([string]$Name) {
    $py = '& ".\.venv\Scripts\python.exe"'
    switch ($Name) {
        'setup' {
            @(
                'python -m venv .venv',
                "$py -m pip install --upgrade pip",
                "$py -m pip install -r requirements.txt -r requirements-dev.txt"
            )
        }
        'env' { @("$py platform_cli.py env") }
        'test' { @("$py -m pytest tests/ -q") }
        'cov' {
            @(
                "$py -m pytest tests/ -q --cov=bench --cov=metrics --cov=sut " +
                '--cov=dashboard --cov=platform_cli --cov-report=term-missing --cov-fail-under=40'
            )
        }
        'lint' {
            @(
                "$py -m ruff check .",
                "$py -m mypy",
                "$py scripts\check_deps_sync.py"
            )
        }
        'lock' {
            @(
                '$env:PIP_TOOLS_CACHE_DIR=".\.pip-tools-cache"',
                "$py -m piptools compile requirements.txt --output-file requirements.lock --strip-extras"
            )
        }
        'perf' {
            @(
                "$py platform_cli.py run --label local-perf --items $Items " +
                "--step-users $($Users -join ' ') --step-duration $Duration"
            )
        }
        'probe' {
            @("$py platform_cli.py probe --label local-n1-probe --items $Items --users 4 --duration $Duration")
        }
        'dash' { @("$py platform_cli.py serve --port 8765") }
        default { @() }
    }
}

function Invoke-Task([string]$Name) {
    # setup 是唯一的例外：它的职责就是从零建出 venv，所以不能要求 venv 已经存在。
    # 原先这里无条件 Assert-Venv，结果在全新克隆上 `-Task setup`（以及 `-Task all`
    # 的第一步）直接 exit 2，还打印「先跑 -Task setup」—— 而人正在跑的就是它。
    # 自相矛盾的提示等于把 bootstrap 路径彻底堵死（实测 SETUP_EXIT=2）。
    # `run.sh:91-98` 把 setup 放在 assert_venv 之前，没有这个问题；这里与它对齐。
    if ($Name -ne 'setup') { Assert-Venv }
    switch ($Name) {
        'setup' {
            $venvDir = Join-Path $RepoRoot '.venv'
            if (-not (Test-Path $venvDir)) {
                Write-Step '建虚拟环境'
                & python -m venv $venvDir
            }
            Write-Step '升级 pip 并安装依赖'
            & $Python -m pip install --upgrade pip
            & $Python -m pip install -r (Join-Path $RepoRoot 'requirements.txt') `
                -r (Join-Path $RepoRoot 'requirements-dev.txt')
            return $LASTEXITCODE
        }
        'env' { & $Python platform_cli.py env; return $LASTEXITCODE }
        'test' { & $Python -m pytest tests/ -q; return $LASTEXITCODE }
        'cov' {
            & $Python -m pytest tests/ -q `
                --cov=bench --cov=metrics --cov=sut --cov=dashboard --cov=platform_cli `
                --cov-report=term-missing --cov-fail-under=40
            return $LASTEXITCODE
        }
        'lint' {
            $rc = 0
            & $Python -m ruff check .; if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }
            & $Python -m mypy; if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }
            & $Python scripts\check_deps_sync.py; if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }
            return $rc
        }
        'lock' {
            $env:PIP_TOOLS_CACHE_DIR = Join-Path $RepoRoot '.pip-tools-cache'
            & $Python -m piptools compile requirements.txt --output-file requirements.lock --strip-extras
            return $LASTEXITCODE
        }
        'perf' {
            & $Python platform_cli.py run --label local-perf --items $Items `
                --step-users $Users --step-duration $Duration
            return $LASTEXITCODE
        }
        'probe' {
            & $Python platform_cli.py probe --label local-n1-probe --items $Items --users 4 --duration $Duration
            return $LASTEXITCODE
        }
        'dash' { & $Python platform_cli.py serve --port 8765; return $LASTEXITCODE }
    }
    return 0
}

Write-Host "仓库根：$RepoRoot"
Write-Host "Python：$Python"
Write-Host ("环境档案：{0}" -f $(if ($Profile -ne '') { $Profile } else { '（未设 PERF_ENV = 默认行为）' }))

if ($Task -eq 'help') {
    Write-Host ''
    Write-Host '可用任务（-Task）：' -ForegroundColor Cyan
    Write-Host '  help   打印将要执行的命令，不真跑（默认）'
    Write-Host '  setup  建 venv 并装依赖'
    Write-Host '  env    显示当前环境档案与生效变量'
    Write-Host '  test   跑平台自身用例'
    Write-Host '  cov    跑用例并套覆盖率门槛'
    Write-Host '  lint   ruff + mypy + 依赖一致性'
    Write-Host '  lock   重新生成 requirements.lock'
    Write-Host '  perf   一轮真压测 + 门禁'
    Write-Host '  probe  N+1 缺陷探针（期望退出码 1）'
    Write-Host '  dash   起看板'
    Write-Host '  all    setup -> lint -> test'
    Write-Host ''
    foreach ($t in @('setup', 'env', 'test', 'cov', 'lint', 'lock', 'perf', 'probe', 'dash')) {
        Write-Host "--- $t ---" -ForegroundColor DarkCyan
        foreach ($c in (Get-Commands $t)) { Write-Host "  $c" }
    }
    Write-Host ''
    Write-Host '注：perf / probe 会真的起被测服务并压测，耗时与 CPU 占用都显著高于其它任务。'
    exit 0
}

if ($Task -eq 'all') {
    foreach ($t in @('setup', 'lint', 'test')) {
        Write-Step "任务：$t"
        $rc = Invoke-Task $t
        if ($rc -ne 0) {
            Write-Host "任务 $t 失败，退出码 $rc —— 停止后续任务。" -ForegroundColor Red
            exit $rc
        }
    }
    Write-Host ''
    Write-Host '全部通过。' -ForegroundColor Green
    exit 0
}

Write-Step "任务：$Task"
foreach ($c in (Get-Commands $Task)) { Write-Host "  $c" -ForegroundColor DarkGray }

$code = Invoke-Task $Task
if ($Task -eq 'probe') {
    # 探针的"成功"就是"门禁拦住"（退出码 1）。不要在这里把它翻成 0 ——
    # 那会把一个真实结论伪装成脚本成功，而 CI 里这个 job 正是靠
    # 这个退出码证明门禁有效。
    Write-Host ''
    Write-Host "探针退出码：$code（1 = 门禁拦住 = 预期结果）" -ForegroundColor Yellow
}
exit $code
