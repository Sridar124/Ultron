Option Explicit

' Hidden per-user launcher with configurable crash recovery.
Const HIDDEN_WINDOW = 0
Const DEFAULT_MAX_RESTARTS = 3
Const RESTART_DELAY_MS = 30000
Const INITIAL_AUDIO_DELAY_MS = 15000

Dim shell, fso, projectDir, logDir, logFile, disabledMarker, pythonw, command
Dim restartCount, exitCode, maxRestarts
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = projectDir & "\logs"
logFile = logDir & "\launcher.log"
disabledMarker = projectDir & "\ultron.disabled"

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
pythonw = projectDir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonw) Then
  WriteLog "Startup stopped: .venv\Scripts\pythonw.exe not found. Create the virtual environment first."
  WScript.Quit 1
End If

' Read max_restarts from config if available
maxRestarts = DEFAULT_MAX_RESTARTS
Dim configFile, configText
configFile = projectDir & "\ultron_config.json"
If fso.FileExists(configFile) Then
  Dim stream
  Set stream = fso.OpenTextFile(configFile, 1, False)
  configText = stream.ReadAll
  stream.Close
  Dim re, matches
  Set re = New RegExp
  re.Pattern = """max_restarts""\s*:\s*(\d+)"
  Set matches = re.Execute(configText)
  If matches.Count > 0 Then
    maxRestarts = CInt(matches(0).SubMatches(0))
  End If
End If

shell.CurrentDirectory = projectDir
command = Quote(pythonw) & " " & Quote(projectDir & "\ultron.py") & " --background"
restartCount = 0

WriteLog "Waiting " & (INITIAL_AUDIO_DELAY_MS \ 1000) & " seconds for Windows audio devices."
WScript.Sleep INITIAL_AUDIO_DELAY_MS

Do
  If fso.FileExists(disabledMarker) Then
    WriteLog "Startup disabled by manual stop request."
    Exit Do
  End If
  WriteLog "Starting ULTRON background process (max_restarts=" & maxRestarts & ")."
  exitCode = shell.Run(command, HIDDEN_WINDOW, True)
  If exitCode = 0 Then
    WriteLog "ULTRON stopped normally."
    Exit Do
  End If

  restartCount = restartCount + 1
  If fso.FileExists(disabledMarker) Then
    WriteLog "Startup disabled by manual stop request."
    Exit Do
  End If
  WriteLog "ULTRON exited with code " & exitCode & ". Restart " & restartCount & " of " & maxRestarts & "."
  If restartCount >= maxRestarts Then
    WriteLog "Startup stopped after bounded crash-restart limit. Check logs\ultron.log."
    Exit Do
  End If
  WScript.Sleep RESTART_DELAY_MS
Loop

Function Quote(value)
  Quote = Chr(34) & value & Chr(34)
End Function

Sub WriteLog(message)
  Dim s
  Set s = fso.OpenTextFile(logFile, 8, True)
  s.WriteLine Now & " " & message
  s.Close
End Sub
