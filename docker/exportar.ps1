# Construye la imagen de la API y la empaqueta para el servidor sin internet.
#
#     .\docker\exportar.ps1
#     .\docker\exportar.ps1 -Version 0.2.0
#
# Necesita PowerShell 7 (`pwsh`). El PowerShell 5 que viene con Windows puede
# no tener Get-FileHash segun la instalacion.
#
# Produce en dist\:
#   teams-transcript-api-<version>.tar   la imagen (docker save)
#   SHA256SUMS.txt                       para verificar la transferencia
#
# El codigo de la aplicacion NO va aqui: se copia por separado con scp y se
# monta como volumen. Ver docker\DESPLIEGUE.md.

param(
    [string]$Version = "0.1.0",
    [string]$Destino = "dist"
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$imagen = "teams-transcript-api:$Version"
$tar = Join-Path $Destino "teams-transcript-api-$Version.tar"

if (-not (Test-Path $Destino)) {
    New-Item -ItemType Directory $Destino | Out-Null
}

Write-Host "==> Construyendo $imagen" -ForegroundColor Cyan
# --platform explicito: no dependemos del valor por defecto de este Docker
# Desktop, que podria no ser el del servidor.
docker build --platform linux/amd64 -f docker/Dockerfile -t $imagen .
if ($LASTEXITCODE -ne 0) { throw "Fallo la construccion de la imagen" }

Write-Host "==> Exportando a $tar" -ForegroundColor Cyan
docker save -o $tar $imagen
if ($LASTEXITCODE -ne 0) { throw "Fallo docker save" }

Write-Host "==> Calculando SHA256" -ForegroundColor Cyan
$hash = (Get-FileHash $tar -Algorithm SHA256).Hash.ToLower()
# Formato de sha256sum(1), para poder verificarlo con `sha256sum -c` en Linux.
"$hash  $(Split-Path -Leaf $tar)" | Set-Content -Path (Join-Path $Destino "SHA256SUMS.txt") -Encoding ascii

$mb = [math]::Round((Get-Item $tar).Length / 1MB, 1)
Write-Host ""
Write-Host "Listo: $tar ($mb MB)" -ForegroundColor Green
Write-Host "SHA256: $hash"
Write-Host ""
Write-Host "Siguiente paso (ver docker\DESPLIEGUE.md):"
Write-Host "  scp $tar $Destino\SHA256SUMS.txt servidor:~/teams_transcript/dist/"
