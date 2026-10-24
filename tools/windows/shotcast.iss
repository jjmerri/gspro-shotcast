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
InfoBeforeFile=..\..\PRIVACY.md
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "startup"; Description: "Start Shotcast when Windows starts (recommended: it records every round)"
Name: "updates"; Description: "Check for updates automatically (asks GitHub every few hours; see the privacy policy)"
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
Source: "..\..\PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\commentator\find_gspro.ps1"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\Shotcast"; Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\shotcast.ico"
Name: "{autodesktop}\Shotcast"; Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\shotcast.ico"; Tasks: desktopicon

[Registry]
; The same value the tray's "Start with Windows" sets; an update keeps whatever the user chose there.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Shotcast"; ValueData: """{app}\python\pythonw.exe"" {#TrayArgs}"; Tasks: startup; Check: not IsUpgrade

; Read by the tray; its menu changes it later, so an update leaves it alone.
Root: HKCU; Subkey: "Software\Shotcast"; ValueType: dword; ValueName: "CheckForUpdates"; ValueData: "1"; Tasks: updates; Check: not IsUpgrade
Root: HKCU; Subkey: "Software\Shotcast"; ValueType: dword; ValueName: "CheckForUpdates"; ValueData: "0"; Tasks: not updates; Check: not IsUpgrade
Root: HKCU; Subkey: "Software\Shotcast"; Flags: uninsdeletekey
; The GSPro folder from the GSPro page: the tray points config.yaml at it when it changes.
Root: HKCU; Subkey: "Software\Shotcast"; ValueType: string; ValueName: "GSProDir"; ValueData: "{code:GSProDir}"; Check: HaveGSProDir

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}"; WorkingDir: "{app}"; Description: "Start Shotcast"; Flags: nowait postinstall skipifsilent
; A silent install is an update from the tray (which quit for it): start it again.
Filename: "{app}\python\pythonw.exe"; Parameters: "{#TrayArgs}{code:HomeArg}"; WorkingDir: "{app}"; Flags: nowait; Check: WizardSilent and Relaunch

[Code]
var
  Upgrade: Boolean;
  GSProPage: TInputQueryWizardPage;
  GSProSearched: Boolean;

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

// ---- the GSPro page: where GSPro is, found if it can be (find_gspro.ps1), else asked for

function FindGSPro: String;
var
  Code: Integer;
  OutFile: String;
  Found: AnsiString;
begin
  Result := '';
  ExtractTemporaryFile('find_gspro.ps1');
  OutFile := ExpandConstant('{tmp}\gspro.txt');
  if Exec('powershell.exe', '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'
      + ExpandConstant('{tmp}\find_gspro.ps1') + '" -Out "' + OutFile + '"', '', SW_HIDE, ewWaitUntilTerminated, Code)
    and LoadStringFromFile(OutFile, Found) then
    Result := Trim(String(Found));
end;

function LooksLikeGSPro(Dir: String): Boolean;
begin
  Result := (Dir <> '') and FileExists(AddBackslash(Dir) + 'Core\GSP\GSPro.exe');
end;

function GSProDir(Param: String): String;
begin
  Result := Trim(GSProPage.Values[0]);
end;

function HaveGSProDir: Boolean;
begin
  Result := GSProDir('') <> '';
end;

procedure BrowseGSPro(Sender: TObject);
var
  Dir: String;
begin
  Dir := GSProPage.Values[0];
  if BrowseForFolder('Choose the folder GSPro is installed in (the one with GSPLauncher.exe):', Dir, False) then
    GSProPage.Values[0] := Dir;
end;

procedure InitializeWizard;
var
  Previous: String;
  Browse: TNewButton;
begin
  // A text box (not a folder page, which won't take an empty answer: GSPro may not be on this PC).
  GSProPage := CreateInputQueryPage(wpSelectTasks, 'GSPro', 'Where is GSPro installed?',
    'Shotcast reads GSPro''s shot log and course files from its folder (the one with GSPLauncher.exe, usually C:\GSProV1).');
  GSProPage.Add('GSPro folder:', False);
  Browse := TNewButton.Create(GSProPage);
  Browse.Parent := GSProPage.Surface;
  Browse.Caption := 'Browse...';
  Browse.Width := ScaleX(80);
  Browse.Height := WizardForm.NextButton.Height;
  Browse.Left := GSProPage.SurfaceWidth - Browse.Width;
  Browse.Top := GSProPage.Edits[0].Top - (Browse.Height - GSProPage.Edits[0].Height) div 2;
  Browse.OnClick := @BrowseGSPro;
  GSProPage.Edits[0].Width := Browse.Left - ScaleX(8) - GSProPage.Edits[0].Left;
  if RegQueryStringValue(HKCU, 'Software\Shotcast', 'GSProDir', Previous) and (Previous <> '') then
  begin
    GSProPage.Values[0] := Previous;
    GSProSearched := True;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Found: String;
begin
  if (CurPageID = GSProPage.ID) and not GSProSearched and not WizardSilent then
  begin
    GSProSearched := True;
    WizardForm.NextButton.Enabled := False;
    GSProPage.SubCaptionLabel.Caption := 'Looking for GSPro...';
    WizardForm.Refresh;
    Found := FindGSPro;
    WizardForm.NextButton.Enabled := True;
    if Found <> '' then
    begin
      GSProPage.Values[0] := Found;
      GSProPage.SubCaptionLabel.Caption := 'Found GSPro here. Shotcast reads its shot log and course files from this folder.';
    end
    else
      GSProPage.SubCaptionLabel.Caption := 'Couldn''t find GSPro on this PC. Browse to the folder it''s installed in '
        + '(the one with GSPLauncher.exe, usually C:\GSProV1), or leave this empty and set it later in Settings.';
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = GSProPage.ID then
  begin
    Dir := GSProDir('');
    if Dir = '' then
      Result := SuppressibleMsgBox('No GSPro folder: Shotcast reads shots from GSPro''s own log instead (no club data) '
        + 'until you set it in the tray''s Settings. Continue?', mbConfirmation, MB_YESNO, IDYES) = IDYES
    else if not LooksLikeGSPro(Dir) then
      Result := SuppressibleMsgBox('There''s no Core\GSP\GSPro.exe in ' + Dir + '. Use this folder anyway?',
        mbConfirmation, MB_YESNO, IDYES) = IDYES;
  end;
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
