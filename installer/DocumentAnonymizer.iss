; Windows installer for Document Anonymizer (Inno Setup 6).
; Built by GitHub Actions (.github/workflows/build-release.yml), never by hand:
;   ISCC.exe /DAppVersion=0.42.0 /DSourceDir=..\dist\DocumentAnonymizer /DOutputDir=..\out installer\DocumentAnonymizer.iss
; Installs per machine into Program Files, adds a Start menu entry and an
; uninstaller. Silent install for CI smoke tests: /VERYSILENT /SUPPRESSMSGBOXES /DIR=...

#ifndef AppVersion
  #error AppVersion must be passed with /DAppVersion=<version>
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\DocumentAnonymizer"
#endif
#ifndef OutputDir
  #define OutputDir "..\out"
#endif

[Setup]
AppId={{6E2B1C4A-8F3D-4B7E-9C21-5A0D7E9F4B13}
AppName=Document Anonymizer
AppVersion={#AppVersion}
AppVerName=Document Anonymizer {#AppVersion}
AppPublisher=Turnkey Financial Solutions
DefaultDirName={autopf}\Document Anonymizer
DefaultGroupName=Document Anonymizer
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=DocumentAnonymizer-Setup-v{#AppVersion}-Windows-x64
SetupIconFile=..\resources\icons\app.ico
UninstallDisplayIcon={app}\DocumentAnonymizer.exe
; x64 only: the bundled torch/onnxruntime/llama.cpp runtimes are x64 builds.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The bundle is ~1.5 GB of mostly already-compressed model data. Non-solid LZMA2
; keeps build time and memory reasonable on the hosted runner.
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Document Anonymizer"; Filename: "{app}\DocumentAnonymizer.exe"
Name: "{group}\Uninstall Document Anonymizer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Document Anonymizer"; Filename: "{app}\DocumentAnonymizer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DocumentAnonymizer.exe"; Description: "{cm:LaunchProgram,Document Anonymizer}"; Flags: nowait postinstall skipifsilent
