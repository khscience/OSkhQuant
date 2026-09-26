; 看海量化回测平台（开源版）安装脚本
;
;   ISCC.exe installer.iss                      （版本号取下面的默认值）
;   ISCC.exe /DMyAppVersion=2.2.0 installer.iss （CI 传入）
;
; 和 V2.1、CS 版是三个独立的软件：AppId、安装目录、开始菜单组、exe 名都不同，
; 可以装在同一台电脑上同时运行。本脚本不写 PATH，也不手工写任何注册表项。
#ifndef MyAppVersion
  #define MyAppVersion "2.2.0"
#endif
#define MyAppName "看海量化回测平台（开源版）"
#define MyAppPublisher "Mr.看海"
#define MyAppURL "https://github.com/khscience/OSkhQuant"
#define MyAppExeName "khQuantOS.exe"
; V2.1 和 CS 共用的 AppId，只用来检测旧版本，绝不改动。
; 它们的安装脚本写的是 AppId={{B39AFBCB-...}}，末尾多了一个 }，实际卸载键名是 {GUID}}_is1
; （32 位安装，在 WOW6432Node 下）；按正确转义的 {GUID}_is1 也查一遍。
#define LegacyAppId "{B39AFBCB-2847-4B4A-B92D-6366E6677A9A}"

[Setup]
AppId={{49FD8240-C072-4D18-B857-77798F22196B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\khQuantOS
DefaultGroupName=khQuantOS
OutputDir=Output
OutputBaseFilename=khQuantOS_Setup_V{#MyAppVersion}
SetupIconFile=icons\stock_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LanguageDetectionMethod=none
ShowLanguageDialog=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "packaging\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\khQuantOS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  UninstallRoot = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\';
  LegacyUninstallKey = UninstallRoot + '{#LegacyAppId}}_is1';
  LegacyUninstallKeyAlt = UninstallRoot + '{#LegacyAppId}_is1';

{ 只读 Inno Setup 自己写的卸载键（卸载时会删掉）；V2.1/CS 另外手写的
  “看海量化回测平台_is1” 卸载后还留着，不能用来判断。
  HKLM 的 32 位视图（WOW6432Node）、64 位视图、HKCU 都查 }
function ReadLegacyVersion(RootKey: Integer; var Version: string): Boolean;
begin
  Result := (RegQueryStringValue(RootKey, LegacyUninstallKey, 'DisplayVersion', Version) and (Version <> ''))
    or (RegQueryStringValue(RootKey, LegacyUninstallKeyAlt, 'DisplayVersion', Version) and (Version <> ''));
end;

function FindLegacyVersion(var Version: string): Boolean;
begin
  Result := ReadLegacyVersion(HKLM32, Version)
    or ReadLegacyVersion(HKLM64, Version)
    or ReadLegacyVersion(HKCU, Version);
  if Result and ((Copy(Version, 1, 1) = 'V') or (Copy(Version, 1, 1) = 'v')) then
    Version := Copy(Version, 2, Length(Version) - 1);
end;

function InitializeSetup(): Boolean;
var
  Version: string;
begin
  Result := True;
  { 读到 2.x 是 V2.1：只提示，不卸载；读到 3.x 是 CS 版：完全不碰 }
  if FindLegacyVersion(Version) and (Copy(Version, 1, 2) = '2.') then
    { 系统消息框大约 36 个汉字宽就折行，每行控制在这个长度以内 }
    MsgBox('检测到本机装有看海量化回测平台 V' + Version + '。' + #13#10#13#10 +
      '开源版 2.2 是独立安装的新程序，可以和 V2.1 同时存在，' + #13#10 +
      '本安装程序不会卸载或改动 V2.1。' + #13#10#13#10 +
      '首次启动开源版时，可以只读导入 V2.1 的设置并复制它的策略。' + #13#10 +
      '不再需要 V2.1 时，可在 Windows「设置 → 应用」里卸载它。',
      mbInformation, MB_OK);
end;
