# Audit of msedge processes: ids, parents, creation time, user-data-dir profile.
$processes = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"
foreach ($p in $processes) {
    $profile = ""
    if ($p.CommandLine -match "user-data-dir=(?<path>[^ ]+)") {
        $profile = $Matches["path"]
    }
    "{0} | parent {1} | {2} | profile={3}" -f $p.ProcessId, $p.ParentProcessId,
        $p.CreationDate.ToString("HH:mm:ss"), $profile
}
