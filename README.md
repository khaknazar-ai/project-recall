# ProjectRecall

A local CLI for project decisions you have reviewed and approved.
Create a project, propose a decision from a report, approve its exact contents,
and recover that decision with the initial task in another process.

- SQLite is the approved memory store.
- Reports create proposals, not approvals.
- Exact approval replay preserves state.
- No Git, provider/model calls, project execution or paid service is needed.

The project is derived from QuestMemory with AI assistance. Attribution and the
no-license status are documented in NOTICE.md. Package-index/name availability
has not been verified.

## Source walkthrough (Windows PowerShell)

Use an existing CPython 3.11+ executable in an approved local runtime namespace
under `C:\AI\projects` or `C:\AI\environments`. Set `$python` to its absolute path.
No runtime dependencies need installing. The checked runtime was CPython 3.11.17.
The source launcher uses `-I -S`: it does not load editable packages, user-site
packages or a QuestMemory installation. The runtime is reused; no original project package or environment is copied.

From this folder:

```powershell
$entry = Join-Path $PWD 'project_recall.py'
function pr {
    & $python -I -B -S -X utf8 $entry @args
    if ($LASTEXITCODE -ne 0) { throw "ProjectRecall exited with code $LASTEXITCODE" }
}
function Get-TextSha256([string] $text) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose() }
}
$demo = 'C:\AI\temp\questmemory-recovery\project-recall-demo-' + [guid]::NewGuid().ToString('N')
$controller = Join-Path $demo 'controller'
$target = Join-Path $demo 'target'
New-Item -ItemType Directory -Path $controller, $target | Out-Null
pr create --project-root $controller --target-root $target --id recall-demo `
    --objective 'Remember approved project decisions.'
```

The directories must be separate and new. The empty target is an inherited
bootstrap binding; ProjectRecall does not execute or modify target content.
Existing unrelated projects are refused. Existing QuestMemory controllers are
not imported or migrated.

Propose a decision in an ordinary report:

```powershell
$report = Join-Path $controller '.questmemory\SESSION_REPORTS\demo.md'
New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
$text = "# Demo`n`n## Decisions Proposed`n- storage: Use one SQLite database per project.`n"
[IO.File]::WriteAllText($report, $text, [Text.UTF8Encoding]::new($false))
$proposals = @(pr propose $report --project-root $controller | ConvertFrom-Json)
if ($proposals.Count -ne 1) { throw 'Expected one proposal.' }
$proposal = $proposals[0]
pr show $proposal.patch_id --project-root $controller
```

Review the statement, source and digests displayed by `show`. If you accept the
decision, explicitly confirm the patch and Core proposal digests together:

```powershell
$approval = "Approve ProjectRecall memory $($proposal.patch_sha256) $($proposal.proposal_sha256)"
$approval
pr approve $proposal.patch_id --sha256 $proposal.patch_sha256 `
    --approval-id demo-memory --approval-statement $approval `
    --approval-text-sha256 (Get-TextSha256 $approval) `
    --confirm-human-approval --project-root $controller
```

Read the persisted value and restore context. Each invocation starts a new Python
process, so the result comes from persisted project state:

```powershell
pr memory --project-root $controller
$restored = pr resume --project-root $controller | ConvertFrom-Json
$restored.task
$restored.memory | ForEach-Object { "$($_.key): $($_.statement)" }
```

The decision is `storage: Use one SQLite database per project.` Restored context
grants neither execution nor replay authority. Keep `$controller` for later
sessions. `status` is a read-only compatibility alias for `resume`, including the
command written to the generated WORKING_COMMANDS.md. Receipts are bound to the
creating runtime path: keep that environment for existing projects. Moving state
or changing runtime paths is not qualified; do not regenerate old receipts. Internal `.questmemory`, `questmemory.toml` and SQLite identifiers are
retained from the extracted storage protocol; the application, package and CLI
are ProjectRecall / `projectrecall` / `project-recall`.

## Local wheel installation

Build only with an existing build toolchain; there are no runtime package
dependencies. The offline check uses CPython's existing bundled pip as a tool,
without installing pip or other dependencies into the fresh environment. It
requires a CPython installation that includes `ensurepip` and its bundled wheel.
Set `$freshEnvRoot` to a new path under the approved recovery namespace and
`$wheel` to the absolute path of the locally built ProjectRecall wheel.

```powershell
& $python -I -B -m venv --without-pip $freshEnvRoot
$freshPython = Join-Path $freshEnvRoot 'Scripts/python.exe'
$bundledPip = & $python -I -B -c "import ensurepip,pathlib; print(next((pathlib.Path(ensurepip.__file__).parent/'_bundled').glob('pip-*.whl')))"
& $freshPython -I -B -c "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('pip',run_name='__main__')" `
    $bundledPip --isolated install --no-index --no-deps --no-cache-dir `
    --disable-pip-version-check --no-compile $wheel
if ($LASTEXITCODE -ne 0) { throw 'Local wheel installation failed.' }
$installedCLI = Join-Path (Split-Path $freshPython) 'project-recall.exe'
```

The workflow above also works through `$installedCLI`: replace the `pr` wrapper's
invocation with `& $installedCLI @args`, retaining its exit-code check. Run it from
a directory outside this source tree. The small source check below is separate from the prior installed-runtime
qualification; running a source launcher is not a fresh-install proof.

## Small CLI check and scope

The included checks use disposable synthetic projects and exercise the public CLI:
memory proposal/approval/cold restore, exact replay, and key owner/path/approval
refusals. Run them with an existing approved Python runtime:

```powershell
& $python -I -B -S tests/test_memory_cli.py
```

Tests create fresh folders under the same approved recovery namespace; they do not
use personal project memory or write state into this repository. No CI workflow
is included. The runtime was checked on Windows with CPython 3.11.17. Other
platforms, crash/power-loss behavior and relocation of existing runtime-bound
state are not qualified.

This CLI supports a new project, its initial task and explicitly approved decision
or constraint memory. It rejects other event families. It is not an execution
engine, repository importer or the complete QuestMemory roadmap. No reuse license
is declared; see NOTICE.md.
