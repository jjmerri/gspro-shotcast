; The Windows installer: a private Python with Shotcast in it (built by build.ps1), installed for
; this Windows user only (no administrator prompt). Settings, the season and logs live in
; %LOCALAPPDATA%\Shotcast, which uninstalling leaves alone.
;   iscc /DAppVersion=1.2.3 /DBuildDir=<build folder> tools\windows\shotcast.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BuildDir
  #define BuildDir "..\..\build\windows"
#endif
#define AppId "{17C2A1D7-CAD8-4E76-949D-1AABA8A216C6}"
#define TrayArgs "-m commentator tray"

[Setup]
AppId={{#AppId}
AppName=Shotcast
AppVersion={#AppVersion}
AppVerName=Shotcast {#AppVersion}
AppPublisher=GSPro Shotcast (open source)
AppPublisherURL=https://github.com/jjmerri/gspro-shotcast
AppSupportURL=https://github.com/jjmerri/gspro-shotcast/issues
AppUpdatesURL=https://github.com/jjmerri/gspro-shotcast/releases
DefaultDirName={localappdata}\Programs\Shotcast
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Shotcast-Setup-{#AppVersion}
SetupIconFile={#BuildDir}\shotcast.ico
UninstallDisplayIcon={app}\shotcast.ico
UninstallDisplayName=Shotcast
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
; Code signing goes here once there's a certificate (SignPath): SignTool=... and SignedUninstaller=yes.

[Tasks]
Name: "startup"; Description: "Start Shotcast when Windows starts (recommended: it records every round)"
Name: "desktopicon"; Description: "Make a desktop shortcut"; Flags: unchecked

[InstallDelete]
; An update replaces the whole app, so packages a new version dropped don't linger.
Type: filesandordirs; Name: "{app}\python"

[UninstallDelete]
; Files Python writes while running (__pycache__) aren't the installer's, so they'd keep the folder.
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\shotcast.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Shotcast"; Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\shotcast.ico"
Name: "{autodesktop}\Shotcast"; Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\shotcast.ico"; Tasks: desktopicon

[Registry]
; The same value the tray's "Start with Windows" sets; an update keeps whatever the user chose there.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Shotcast"; ValueData: """{app}\python\pythonw.exe"" {#TrayArgs}"; Tasks: startup; Check: not IsUpgrade

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; Description: "Start Shotcast"; Flags: nowait postinstall skipifsilent
; A silent install is an update from the tray (which quit for it): start it again.
Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}{code:HomeArg}"; WorkingDir: "{app}"; Flags: nowait; Check: WizardSilent and Relaunch

[Code]
var
  Upgrade: Boolean;

function IsUpgrade: Boolean;
begin
  Result := Upgrade;
end;

// The tray's own folder when it isn't the default (the updater passes /home=<folder>).
function HomeArg(Param: String): String;
begin
  Result := ExpandConstant('{param:home|}');
  if Result <> '' then
    Result := ' --home "' + Result + '"';
end;

function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:nolaunch|0}') <> '1';
end;

// The tray and its booth run from the app's python folder: stop them so their files can be replaced.
procedure StopShotcast;
var
  Code: Integer;
  Dir: String;
begin
  Dir := ExpandConstant('{localappdata}\Programs\Shotcast\python\');
  Exec('powershell.exe', '-NoProfile -NonInteractive -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | '
    + 'Where-Object { $_.Path -like ''' + Dir + '*'' } | Stop-Process -Force"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function InitializeSetup: Boolean;
begin
  Upgrade := RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1', 'UninstallString');
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopShotcast;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopShotcast;
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Shotcast');
  end;
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox('Your settings, season database and backups are still in ' + ExpandConstant('{localappdata}\Shotcast')
      + '. Delete that folder too if you don''t want them.', mbInformation, MB_OK);
end;
