; Compile with /DAppSource=<absolute folder> /DAppVersion=<version>.
; Release signing is performed and checked by build_installer.ps1.
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
[Setup]
AppId=ShellieSoftwareTools.FrontDesk
AppName=FrontDesk
AppVersion={#AppVersion}
AppPublisher=ShellieSoftwareTools
DefaultDirName={localappdata}\Programs\ShellieSoftwareTools\FrontDesk
DefaultGroupName=FrontDesk
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=FrontDesk-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayIcon={app}\FrontDesk.exe
SignedUninstaller=yes
SignTool=frontdesk
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\FrontDesk"; Filename: "{app}\FrontDesk.exe"
Name: "{group}\Uninstall FrontDesk"; Filename: "{uninstallexe}"
[Run]
Filename: "{app}\FrontDesk.exe"; Description: "Start FrontDesk"; Flags: nowait postinstall skipifsilent
; Deliberately no UninstallDelete: customer conversations and models survive.
