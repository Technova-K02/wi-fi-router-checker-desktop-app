; Router Checker installer (Inno Setup 6). Build it with packaging/build.py, which
; passes AppVersion, Exe (dist\RouterChecker.exe), Icon and OutputDir.
;
; Installs for the current user only (no admin rights) into
; %LOCALAPPDATA%\Programs\Router Checker. The uninstaller closes the app and
; removes everything it wrote to the registry; the data folder
; %LOCALAPPDATA%\RouterChecker goes only if you say so.

#ifndef AppVersion
  #error Build with packaging/build.py (it passes AppVersion, Exe, Icon and OutputDir).
#endif

#define AppName "Router Checker"
#define AppExe "RouterChecker.exe"
#define AppGuid "D0E20EA0-5F01-49F5-AE3A-5D9588BDECFD"
; Keep these in step with the app (ui/app.py APP_ID, core/startup.py RUN_VALUE).
#define AppUserModelId "RouterChecker.RouterChecker"
#define RunValue "RouterChecker"

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile={#Icon}
OutputDir={#OutputDir}
OutputBaseFilename=RouterChecker-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; The app is closed with --exit first (see PrepareToInstall); this is the fallback.
CloseApplications=yes
RestartApplications=no

[Tasks]
; Only offered on a first install: afterwards Settings > Start with Windows decides.
Name: "startup"; Description: "Start {#AppName} when I sign in"; Check: IsFirstInstall
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#Exe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The app id lets Windows group the window and the notifications with the shortcut.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Registry]
; The same value Settings > Start with Windows writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#RunValue}"; ValueData: """{app}\{#AppExe}"" --minimized"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  ApprovedKey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';
  AppIdKey = 'Software\Classes\AppUserModelId\{#AppUserModelId}';
  NotificationsKey = 'Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{#AppUserModelId}';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1';

function IsFirstInstall: Boolean;
begin
  Result := not RegKeyExists(HKCU, UninstallKey);
end;

{ Ask the running copy to close and wait until the .exe is free (up to 15 s). }
procedure CloseRunningApp;
var
  Exe: String;
  ResultCode: Integer;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
    Exec(Exe, '--exit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    CloseRunningApp;
    RegDeleteValue(HKCU, RunKey, '{#RunValue}');
    RegDeleteValue(HKCU, ApprovedKey, '{#RunValue}');
    RegDeleteKeyIncludingSubkeys(HKCU, AppIdKey);
    RegDeleteKeyIncludingSubkeys(HKCU, NotificationsKey);
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\RouterChecker');
    if DirExists(DataDir) and not UninstallSilent and
       (MsgBox('Also delete your routers, settings and history?' #13#10#13#10 + DataDir,
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(DataDir, True, True, True);
  end;
end;
