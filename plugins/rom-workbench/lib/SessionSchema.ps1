# Session JSONL reader/writer.
# Format: schemas/session.schema.json. First line is `{kind:"meta",...}`; subsequent
# lines are timestamped events.

#requires -Version 7.0

# A writer keeps a single file handle open for the lifetime of recording.
class RpSessionWriter {
    hidden [System.IO.StreamWriter] $_w
    hidden [string] $_path
    hidden [int] $_count

    RpSessionWriter([string] $path) {
        $dir = Split-Path -Parent $path
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        # Open append; UTF-8 no BOM.
        $stream = [System.IO.File]::Open($path, 'Append', 'Write', 'Read')
        $this._w = New-Object System.IO.StreamWriter($stream, [System.Text.UTF8Encoding]::new($false))
        $this._w.AutoFlush = $false
        $this._path = $path
        $this._count = 0
    }

    [void] WriteMeta([hashtable] $meta) {
        $rec = @{ v = 1; kind = 'meta' } + $meta
        $this._WriteRecord($rec)
    }

    [void] WriteSwitch([double] $t, [int] $n, [bool] $on) {
        $this._WriteRecord(@{ t = $t; kind = 'switch'; n = $n; on = $on })
    }

    [void] WriteNote([double] $t, [string] $msg) {
        $this._WriteRecord(@{ t = $t; kind = 'note'; msg = $msg })
    }

    hidden [void] _WriteRecord([hashtable] $rec) {
        $json = $rec | ConvertTo-Json -Compress -Depth 6
        $this._w.WriteLine($json)
        $this._count++
        if (($this._count % 256) -eq 0) { $this._w.Flush() }
    }

    [void] Flush() { $this._w.Flush() }
    [int]  RecordCount() { return $this._count }

    [void] Close() {
        if ($null -ne $this._w) {
            $this._w.Flush()
            $this._w.Dispose()
            $this._w = $null
        }
    }
}

function New-RpSessionWriter {
    param([Parameter(Mandatory)][string] $Path)
    return [RpSessionWriter]::new($Path)
}

function Read-RpSessionMeta {
    param([Parameter(Mandatory)][string] $Path)
    $firstLine = Get-Content -LiteralPath $Path -TotalCount 1
    if (-not $firstLine) { throw "Empty session file: $Path" }
    $obj = $firstLine | ConvertFrom-Json
    if ($obj.kind -ne 'meta') { throw "First line of $Path is not a meta record." }
    return $obj
}
