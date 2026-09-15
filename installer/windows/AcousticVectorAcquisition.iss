#define MyAppName "Acoustic Vector Acquisition System"
#define MyAppExeName "AcousticVectorAcquisition.exe"
#define MyAppVersion GetEnv("ACOUSTIC_VERSION")
#define MyNumericVersion GetEnv("ACOUSTIC_NUMERIC_VERSION")
#define MyBuildDir GetEnv("ACOUSTIC_WINDOWS_DIST")
#define MySignatureSuffix GetEnv("ACOUSTIC_WINDOWS_SIGNATURE_SUFFIX")

[Setup]
AppId={{5A05F40A-62B7-49CC-9272-CE8CFFCE89F2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyNumericVersion}
VersionInfoDescription={#MyAppName}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={autopf}\AcousticVectorAcquisition
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes
OutputDir=..\..\release-assets
OutputBaseFilename=AcousticVectorAcquisition-{#MyAppVersion}-windows-x64{#MySignatureSuffix}-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall

[UninstallDelete]
; Intentionally empty. User recordings and configuration live outside {app} and are never deleted.
