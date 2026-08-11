; Inno Setup script for Kokin's Swiss Downloader.
;
; Build with:  ISCC installer.iss
; (build.bat does this for you, after PyInstaller.)
;
; ── Why an installer at all ───────────────────────────────────────────────────
; The app is built with PyInstaller --onedir, because --onefile re-extracted
; 1.45 GB to %TEMP% on every launch and put time-to-window at ~2 minutes. But
; --onedir means the artifact is a folder of 5,000+ files, and the old in-app
; updater worked by moving one .exe over another — which cannot work when the
; app is a folder, and cannot work at all on files Windows has locked because
; the app is running them.
;
; An installer solves exactly that: it is a single .exe (so updater.py's
; asset scan finds it again with no code change), and replacing files that are
; in use is the one thing installers are actually good at.
;
; ── Why per-user, not Program Files ───────────────────────────────────────────
; PrivilegesRequired=lowest installs to %LOCALAPPDATA%\Programs with NO UAC
; prompt, on first install and on every update. That matters more than it
; sounds: a self-update that throws a UAC dialog every time is a self-update
; people stop clicking, and this app updates often. The tradeoff is that the
; install is for this user only, which for an app shared with friends as a
; direct download is the right side of the trade.

#define AppName        "Kokin's Swiss Downloader"
#define AppExeName     "Swiss Downloader.exe"
#define AppPublisher   "kokin-blip"
#define AppURL         "https://github.com/kokin-blip/kokin-swiss-downloader"
; Read the version straight out of the built exe so it can never disagree with
; version.py. PyInstaller does not stamp a version resource unless asked, so
; fall back to a define that build.bat passes in with /DAppVersion=...
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Where PyInstaller's --onedir output lives. build.bat uses the default and needs
; no change. CI overrides it with /DAppDir=... and builds to a short path,
; because the runner's workspace prefix is 36 characters longer than a local
; checkout — enough to push the deepest bundled Chromium files past Windows'
; 260-char MAX_PATH. ISCC then compresses almost the whole tree and aborts with
; "The system cannot find the path specified", which names neither the file nor
; the real cause.
#ifndef AppDir
  #define AppDir "dist\Swiss Downloader"
#endif

[Setup]
; A stable AppId is what makes the next release an UPGRADE rather than a second
; copy installed alongside. Never change it.
AppId={{7C4F1E92-3B6A-4D58-9E21-AC4F0D8B5E13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}

; Per-user, no elevation. See the header.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Swiss Downloader
DefaultGroupName=Swiss Downloader
DisableProgramGroupPage=yes
; Nobody reads a licence page for a tool they were handed by a friend, and an
; extra click is an extra chance to abandon an update.
DisableWelcomePage=yes
DisableReadyPage=no

OutputDir=dist
OutputBaseFilename=Swiss-Downloader-v{#AppVersion}-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

; LZMA2/max on a 1.5 GB payload is slow to compress but produces the smallest
; download, and this is built once per release and downloaded many times.
Compression=lzma2/max
SolidCompression=yes
; The payload is far past the 2 GB internal-structure ceiling of the default.
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; Close the app if it is running, and put it back afterwards. This is the whole
; mechanism the old move-the-exe batch file was badly reinventing.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=yes

WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole --onedir output. recursesubdirs pulls in _internal, which is where
; PyInstaller puts ffmpeg, the ui folder and assets\models\u2net.onnx.
Source: "{#AppDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Deliberately NOT `skipifsilent`. A silent run is the self-update path, and the
; whole point there is that the app comes back up on its own — skipping this on
; silent would leave the user staring at a closed window wondering what happened.
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall

[UninstallDelete]
; PyInstaller writes __pycache__ and the app writes nothing here, but an
; interrupted update can leave stragglers that would otherwise orphan the folder.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
