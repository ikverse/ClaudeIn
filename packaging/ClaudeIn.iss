; Inno Setup recipe for ClaudeIn-Setup.exe. Built by packaging\build.py, which passes AppVersion, SourceDir, OutputDir, Icon.
; Installs for the current Windows user only: no administrator rights, no UAC question.
; With /DTestBuild (build.py --test): "ClaudeIn Test", its own app ID and folder, and it never stops or changes the real
; ClaudeIn (no shortcuts, no restart task, nothing stopped). For trying a build on the owner's PC.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\ClaudeIn"
#endif
#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif
#define Publisher "ikverse"
#define Site "https://github.com/ikverse/ClaudeIn"

[Setup]
#ifdef TestBuild
AppId={{2B9D7E44-0C3F-4A61-8E15-7F0B6C2D9A30}
AppName=ClaudeIn Test
DefaultDirName={autopf}\ClaudeIn-Test
OutputBaseFilename=ClaudeIn-Test-Setup-{#AppVersion}
#else
AppId={{6F3B0C1E-5D2A-4B7E-9A41-C1A0D1E7F2B8}
AppName=ClaudeIn
DefaultDirName={autopf}\ClaudeIn
OutputBaseFilename=ClaudeIn-Setup-{#AppVersion}
#endif
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#Site}
AppSupportURL={#Site}
AppUpdatesURL={#Site}/releases
AppCopyright=© {#Publisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription=ClaudeIn installer: lets your apps use Claude through this PC
VersionInfoProductName=ClaudeIn
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=© {#Publisher}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\ClaudeIn.exe
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
#ifdef Icon
SetupIconFile={#Icon}
#endif

#ifndef TestBuild
[Tasks]
Name: "menuicon"; Description: "Add ClaudeIn to the Start menu"
Name: "startup"; Description: "Start ClaudeIn when I sign in to Windows"
#endif

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

#ifndef TestBuild
[Icons]
; The same names ClaudeIn's own switch uses, so "Start with Windows" in ClaudeIn and this installer agree.
Name: "{autoprograms}\ClaudeIn"; Filename: "{app}\ClaudeIn.exe"; Comment: "ClaudeIn: lets your apps use Claude through this PC"; Tasks: menuicon
Name: "{userstartup}\ClaudeIn"; Filename: "{app}\ClaudeIn.exe"; Tasks: startup

[Run]
; --setup refreshes the shortcuts' icon and makes the restart task match its switch (clearing the every-minute task of
; older versions).
Filename: "{app}\ClaudeIn.exe"; Parameters: "--setup"; Flags: runhidden waituntilterminated; Tasks: menuicon startup
Filename: "{app}\ClaudeIn.exe"; Description: "Start ClaudeIn now"; Flags: nowait postinstall skipifsilent
; An update started by ClaudeIn itself (/UPDATE=1, quiet): start the new ClaudeIn again when done.
Filename: "{app}\ClaudeIn.exe"; Flags: nowait; Check: IsUpdate
#endif

[Code]
// True when ClaudeIn started this installer to update itself (it passes /UPDATE=1).
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

#ifndef TestBuild
// Is any ClaudeIn.exe running? (tasklist's answer, read from a file)
function ClaudeInRunning: Boolean;
var
  ResultCode: Integer;
  Answer: AnsiString;
  F: String;
begin
  F := ExpandConstant('{%TEMP}\claudein-tasklist.txt');
  Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ClaudeIn.exe" /NH > "' + F + '"', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Result := LoadStringFromFile(F, Answer) and (Pos('claudein.exe', Lowercase(String(Answer))) > 0);
  DeleteFile(F);
end;

// Closes a running ClaudeIn politely: `ClaudeIn.exe --quit` asks it to close as Quit in the tray does, and waits up to
// 20 seconds. Only if it is still running after that is it stopped by force (an older version has no --quit).
procedure StopClaudeIn;
var
  ResultCode: Integer;
  Exe: String;
begin
  if not ClaudeInRunning then
    Exit;
  Exe := ExpandConstant('{app}\ClaudeIn.exe');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ClaudeInRunning then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ClaudeIn.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Before files are replaced (an update), close a running ClaudeIn. The quit flag keeps its restart task from bringing it
// back in the middle of the update; the next normal start clears the flag.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Folder: String;
begin
  Folder := ExpandConstant('{%USERPROFILE}\.claudein');
  if ForceDirectories(Folder) then
    SaveStringToFile(Folder + '\quit.flag', 'Stopped by the installer. Cleared at the next normal start.', False);
  StopClaudeIn;
  Result := '';
end;

// Uninstalling: close ClaudeIn politely, then remove the Startup shortcut and restart task, only if they start this program.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopClaudeIn;
    Exec(ExpandConstant('{app}\ClaudeIn.exe'), '--remove-startup', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
#endif
