$pythonPath = "C:\Users\salma\.arena\venv\Scripts\python.exe"
while ($true) {
    $output = & $pythonPath -m arena_cli.main submit 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Output "Submission succeeded!"
        break
    }
    $outputText = Out-String -InputObject $output
    if ($outputText -like "*quota exceeded*" -or $outputText -like "*already submitted*") {
        Write-Output "Quota not yet refunded or queue busy, waiting 30 seconds..."
        Start-Sleep -Seconds 30
    } else {
        Write-Output "Failed with other error: $outputText"
        break
    }
}
