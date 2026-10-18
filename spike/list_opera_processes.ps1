# Inventory of opera.exe processes: pid, start time, user-data-dir (if any).
Get-CimInstance Win32_Process -Filter "Name='opera.exe'" | ForEach-Object {
    $profile = "<personal or none>"
    if ($_.CommandLine -match "user-data-dir=(?<path>[^ ]+)") {
        $profile = $Matches["path"]
    }
    "{0} | {1} | {2}" -f $_.ProcessId, $_.CreationDate.ToString("HH:mm:ss"), $profile
}
