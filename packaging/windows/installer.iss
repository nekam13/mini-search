; Inno Setup script for the Mini Search Windows installer.
;
; Build with:  iscc /DAppVersion=7.9 packaging\windows\installer.iss
; The portable zip is produced by build_windows.ps1 whether or not Inno Setup is
; present; this file only wraps the PyInstaller bundle into a Setup.exe.

#ifndef AppVersion
  #define AppVersion "7.9"
#endif

#define AppName "Mini Search"
#define AppExe "mini-search.exe"
#define SourceDir "build\pyinstaller-dist\mini-search"

[Setup]
AppId={{7B4A9C2E-3E1F-4B77-9C21-7A1D5C8F0B92}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=nekam13
AppPublisherURL=https://github.com/nekam13/mini-search
DefaultDirName={autopf}\MiniSearch
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=MiniSearch-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Vytvořit zástupce na ploše"; GroupDescription: "Zástupci:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Spustit {#AppName}"; Flags: nowait postinstall skipifsilent
