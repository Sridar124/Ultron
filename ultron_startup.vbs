Option Explicit

' Hidden per-user launcher with bounded crash recovery.
Const HIDDEN_WINDOW = 0
Const MAX_RESTARTS = 3
Const RESTART_DELAY_MS = 30000
Const INITIAL_AUDIO_DELAY_MS = 15000

Dim shell, fso, projectDir, logDir, logFile, disabledMarker, pythonw, command
Dim restartCount, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = projectDir & "\logs"
logFile = logDir & "\launcher.log"
disabledMarker = projectDir & "\ultron.disabled"

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
pythonw = projectDir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonw) Then
  WriteLog "Startup stopped: .venv\Scripts\pythonw.exe was not found. Create the project virtual environment first."
  WScript.Quit 1
End If

shell.CurrentDirectory = projectDir
command = Quote(pythonw) & " " & Quote(projectDir & "\ultron.py") & " --background"
restartCount = 0

' Audio endpoints and microphone privacy brokers can take a few seconds to
' become available immediately after Windows sign-in. This still runs inside
' the interactive user's Startup session; it simply waits before first use.
WriteLog "Waiting " & (INITIAL_AUDIO_DELAY_MS \ 1000) & " seconds for Windows audio devices."
WScript.Sleep INITIAL_AUDIO_DELAY_MS

Do
  If fso.FileExists(disabledMarker) Then
    WriteLog "Startup disabled by manual stop request."
    Exit Do
  End If
  WriteLog "Starting ULTRON background process."
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
  WriteLog "ULTRON exited with code " & exitCode & ". Restart " & restartCount & " of " & MAX_RESTARTS & "."
  If restartCount >= MAX_RESTARTS Then
    WriteLog "Startup stopped after bounded crash-restart limit. Check logs\ultron.log."
    Exit Do
  End If
  WScript.Sleep RESTART_DELAY_MS
Loop

Function Quote(value)
  Quote = Chr(34) & value & Chr(34)
End Function

Sub WriteLog(message)
  Dim stream
  Set stream = fso.OpenTextFile(logFile, 8, True)
  stream.WriteLine Now & " " & message
  stream.Close
End Sub
