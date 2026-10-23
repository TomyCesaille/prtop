$ErrorActionPreference = 'Stop'

$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python = Join-Path $toolsDir 'python\python.exe'

# One shim, `prtop`, runs the bundled Python on the bundled prtop. The
# `.ignore` files next to python.exe and pythonw.exe keep Chocolatey from
# shimming those two, so the bundled Python never shadows another one.
Install-BinFile -Name 'prtop' -Path $python -Command '-m prtop'
