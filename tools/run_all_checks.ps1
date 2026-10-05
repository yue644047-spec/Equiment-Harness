<#
  一键自检门禁（防跑偏闸门）
  用法： powershell -ExecutionPolicy Bypass -File tools\run_all_checks.ps1
  通过标准：下面 7 个套件全部 exit=0；任一失败则本脚本 exit=1。
  说明：全部套件都是只读的（不写库、不改文件），无凭据也能跑（评测服务为 mock 模式）。
#>
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot          # 仓库根目录（tools 的上一级）
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$py = $null
foreach ($cand in @('D:\ProgramData\anaconda3\python.exe', (Get-Command python -ErrorAction SilentlyContinue).Source, (Get-Command py -ErrorAction SilentlyContinue).Source)) {
    if ($cand -and (Test-Path $cand)) { $py = $cand; break }
}
if (-not $py) { Write-Host '[FATAL] 未找到 Python 3 解释器' -ForegroundColor Red; exit 2 }
Write-Host ("门禁开始：" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "  python=" + $py)
Write-Host ('-' * 78)

$suites = @(
    @{ name = 'check_drift';        args = @('tools\check_drift.py');         hint = '文档/代码里的 DSL 引用与基线一致（防跑偏）' },
    @{ name = 'validate_dsl';       args = @('tools\validate_dsl.py');        hint = 'DSL 结构/连线/变量/代码语法' },
    @{ name = 'test_code_nodes';    args = @('tools\test_code_nodes.py');     hint = 'DSL 内嵌评分与质检逻辑行为断言' },
    @{ name = 'test_contract';      args = @('tools\test_contract.py');       hint = 'DSL ↔ 评测服务跨文件契约' },
    @{ name = 'check_corpus';       args = @('tools\check_corpus.py');        hint = '语料编号/版本一致性与作废值隔离' },
    @{ name = 'kb_bridge_selftest'; args = @('kb_bridge\selftest.py');        hint = '检索契约层（协议/片段/引用/过滤）' },
    @{ name = 'eval_selftest';      args = @('eval_service\tests\selftest.py'); hint = '评测服务全链路（mock）' }
)

$failed = @()
foreach ($s in $suites) {
    $out = & $py @($s.args) 2>&1
    $code = $LASTEXITCODE
    $tail = ($out | Where-Object { $_ -match '项|PASS|校验|错误|自测结束|契约测试' } | Select-Object -Last 1)
    if ($code -eq 0) {
        Write-Host ("  [PASS] {0,-20} {1}" -f $s.name, $tail) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0,-20} exit={1}" -f $s.name, $code) -ForegroundColor Red
        $out | Select-Object -Last 12 | ForEach-Object { Write-Host ("         " + $_) }
        $failed += $s.name
    }
}

Write-Host ('-' * 78)
if ($failed.Count -eq 0) {
    Write-Host '门禁通过：7 个套件全部 exit=0。' -ForegroundColor Green
    exit 0
}
Write-Host ('门禁未通过：' + ($failed -join '、') + '。修完再继续，禁止跳过。') -ForegroundColor Red
exit 1
