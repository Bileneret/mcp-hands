# Stop only OUR Opera instances (those carrying the work profile in user-data-dir).
$ours = Get-CimInstance Win32_Process -Filter "Name='opera.exe'" |
    Where-Object { $_.CommandLine -like "*opera_profile*" }
foreach ($p in $ours) {
    Write-Output ("kill " + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1
$left = (Get-CimInstance Win32_Process -Filter "Name='opera.exe'" |
    Where-Object { $_.CommandLine -like "*opera_profile*" } | Measure-Object).Count
Write-Output ("our opera processes left: " + $left)
