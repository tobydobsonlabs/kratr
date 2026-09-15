; KRATR — Inno Setup script
; Builds a single self-contained installer (KRATR-Setup-v<version>.exe) that friends
; can run with no admin rights and no dependencies to install by hand.
;
; Compile with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\kratr.iss
;
; Expects, relative to this script's parent (the project root):
;   dist-friend\KRATR.exe        the GUI     (built by PyInstaller from crate.spec)
;   dist-friend\kratr-cli.exe    the console twin (doctor / audit / verify)
;   vendor\ffmpeg\ffmpeg.exe     bundled audio engine  (installed beside KRATR.exe)
;   vendor\ffmpeg\ffprobe.exe    bundled audio inspector
;   vendor\ffmpeg\ffmpeg-LICENSE.txt
;   crate\resources\kratr-icon.ico

#define AppName "KRATR"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppPublisher "KRATR"
#define AppExeName "KRATR.exe"
#define SourceRoot ".."

[Setup]
AppId={{7C7F5B2E-3E2A-4E7B-9E4D-4B7A0C4B2A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install: no UAC prompt, nothing lands in Program Files, friends can just run it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#SourceRoot}\installer\out
OutputBaseFilename=KRATR-Setup-v{#AppVersion}
SetupIconFile={#SourceRoot}\crate\resources\kratr-icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#SourceRoot}\installer\assets\LICENSE-notice.txt
; A first-time reader may not know what KRATR is; give the welcome page some words.
AppComments=DJ track intake for rekordbox — convert, quality-check, file, import, tag and colour in one pass.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#SourceRoot}\dist-friend\KRATR.exe";      DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\dist-friend\kratr-cli.exe";  DestDir: "{app}"; Flags: ignoreversion
; ffmpeg lives right beside KRATR.exe so KRATR finds it with zero PATH setup.
Source: "{#SourceRoot}\vendor\ffmpeg\ffmpeg.exe";   DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\vendor\ffmpeg\ffprobe.exe";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\vendor\ffmpeg\ffmpeg-LICENSE.txt"; DestDir: "{app}"; DestName: "ffmpeg-LICENSE.txt"; Flags: ignoreversion
Source: "{#SourceRoot}\installer\assets\README-first.txt";  DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\KRATR";                Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall KRATR";      Filename: "{uninstallexe}"
Name: "{userdesktop}\KRATR";          Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch KRATR now"; Flags: nowait postinstall skipifsilent
