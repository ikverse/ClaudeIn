; Inno Setup recipe for ClaudeIn-Setup.exe. Built by packaging\build.py, which passes AppVersion, SourceDir, OutputDir, Icon.
; Installs for the current Windows user only: no administrator rights, no UAC question.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\ClaudeIn"
#endif
#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif

[Setup]
AppId={{6F3B0C1E-5D2A-4B7E-9A41-C1A0D1E7F2B8}
AppName=ClaudeIn
AppVersion={#AppVersion}
AppPublisher=ClaudeIn
DefaultDirName={autopf}\ClaudeIn
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ClaudeIn-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\ClaudeIn.exe
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
#ifdef Icon
SetupIconFile={#Icon}
#endif

[Tasks]
Name: "menuicon"; Description: "Add ClaudeIn to the Start menu"
Name: "startup"; Description: "Start ClaudeIn when I sign in to Windows (it also restarts itself within a minute if it stops)"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; The same names ClaudeIn's own switch uses, so "Start with Windows" in ClaudeIn and this installer agree.
Name: "{autoprograms}\ClaudeIn"; Filename: "{app}\ClaudeIn.exe"; Comment: "ClaudeIn: lets your apps use Claude through this PC"; Tasks: menuicon
Name: "{userstartup}\ClaudeIn"; Filename: "{app}\ClaudeIn.exe"; Tasks: startup

[Run]
; --setup adds the restart task when the Startup shortcut exists, and refreshes the shortcuts' icon.
Filename: "{app}\ClaudeIn.exe"; Parameters: "--setup"; Flags: runhidden waituntilterminated; Tasks: menuicon startup
Filename: "{app}\ClaudeIn.exe"; Description: "Start ClaudeIn now"; Flags: nowait postinstall skipifsilent
; An update started by ClaudeIn itself (/UPDATE=1, quiet): start the new ClaudeIn again when done.
Filename: "{app}\ClaudeIn.exe"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{app}\ClaudeIn.exe"; Parameters: "--remove-startup"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartup"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ClaudeIn.exe"; Flags: runhidden waituntilterminated; RunOnceId: "StopClaudeIn"

[Code]
// True when ClaudeIn started this installer to update itself (it passes /UPDATE=1).
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

// Before files are replaced (an update), stop a running ClaudeIn. The quit flag keeps its "start again within a minute"
// task from bringing it back in the middle of the update; the next normal start clears the flag.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Folder: String;
begin
  Folder := ExpandConstant('{%USERPROFILE}\.claudein');
  if ForceDirectories(Folder) then
    SaveStringToFile(Folder + '\quit.flag', 'Stopped by the installer. Cleared at the next normal start.', False);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ClaudeIn.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
