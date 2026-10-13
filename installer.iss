; Muse Bridge — Inno Setup installer script.
; Requires Inno Setup 6 (https://jrsoftware.org/isinfo.php) on the build PC.
; Build the EXE first with build_windows.bat, then compile this script.
; Produces a real installer: first-run wizard, Start Menu entry, version
; info, optional launch-on-startup, clean uninstall.

#define MyAppName "Muse Bridge"
#define MyAppVersion "2.4.0"
#define MyAppPublisher "Muse Bridge"
#define MyAppExeName "MuseBridge.exe"

[Setup]
AppId={{3F2A1B4C-9D8E-4F7A-B6C5-8E9F0A1B2C3D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MuseBridge
DefaultGroupName=Muse Bridge
OutputBaseFilename=MuseBridge-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Clean uninstall: remove everything we installed, leave user data alone
; (user data lives in %APPDATA%\MuseBridge and survives uninstall).

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Launch on Windows startup"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\MuseBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Muse Bridge"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Muse Bridge"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Muse Bridge"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon
Name: "{userstartup}\Muse Bridge"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "{cm:LaunchProgram,Muse Bridge}"; \
    Flags: nowait postinstall skipifsilent
