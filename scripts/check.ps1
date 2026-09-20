$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([Parameter(Mandatory = $true)][scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Invoke-Checked { uv run --locked ruff check . }
Invoke-Checked { uv run --locked ruff format --check . }
Invoke-Checked { uv run --locked lint-imports }
Invoke-Checked { uv run --locked mypy --strict src/cua }
Invoke-Checked { uv run --locked pytest }
