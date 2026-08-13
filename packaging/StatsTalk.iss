#ifndef AppVersion
  #define AppVersion "0.9.0-beta"
#endif

#ifndef SourceDir
  #define SourceDir "..\\dist"
#endif

#ifndef OutputDir
  #define OutputDir "..\\release"
#endif

[Setup]
AppId={{D5D041F9-B62B-49A6-82DC-49E472539588}
AppName=StatsTalk
AppVersion={#AppVersion}
AppPublisher=StatsTalk
DefaultDirName={localappdata}\Programs\StatsTalk
DefaultGroupName=StatsTalk
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=StatsTalk-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\StatsTalk.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Files]
Source: "{#SourceDir}\StatsTalk.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\StatsTalk"; Filename: "{app}\StatsTalk.exe"
Name: "{autodesktop}\StatsTalk"; Filename: "{app}\StatsTalk.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\StatsTalk.exe"; Description: "Launch StatsTalk"; Flags: nowait postinstall skipifsilent

[Code]
var
  RemoveUserData: Boolean;

function WebView2Installed: Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F1E7E1A1-2A73-4AA7-8A76-8B57A5A9A7E0}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1E7E1A1-2A73-4AA7-8A76-8B57A5A9A7E0}', 'pv', Version) or
    RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1E7E1A1-2A73-4AA7-8A76-8B57A5A9A7E0}', 'pv', Version);
end;

procedure InitializeWizard;
begin
  if not WebView2Installed then
    MsgBox(
      'Microsoft Edge WebView2 Runtime was not detected.' + #13#10 + #13#10 +
      'StatsTalk will open its token-protected local interface in your default browser. ' +
      'For the native desktop window, install WebView2 Runtime from Microsoft.',
      mbInformation, MB_OK);
end;

function InitializeUninstall: Boolean;
begin
  RemoveUserData := MsgBox(
    'Also remove StatsTalk configuration and local application data?' + #13#10 + #13#10 +
    'This includes the DPAPI-protected API key, encrypted dataset restore reference, ' +
    'temporary workspaces, telemetry identifier, and queued crash-report preview.' + #13#10 + #13#10 +
    'Your original datasets and exported API-key backup files will not be deleted.',
    mbConfirmation, MB_YESNO) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
    DelTree(ExpandConstant('{userappdata}\StatsTalk'), True, True, True);
end;
