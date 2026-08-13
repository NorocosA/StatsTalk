StatsTalk Portable 0.9 beta
===========================

Run StatsTalk.exe directly. No installation or administrator access is required.

Data policy
-----------
The portable.marker file makes StatsTalk store configuration, the DPAPI-protected API
key, encrypted restore metadata, temporary workspaces, and optional crash-report state
inside the Data folder next to StatsTalk.exe. Original datasets remain where you put
them. API-key backup files are saved only to a location you explicitly choose.

The DPAPI-protected key remains bound to the Windows account that created it. Before
moving this folder to another account or machine, export a password-protected key backup
from Settings. Do not copy the Data folder as a substitute for that backup.

WebView2
--------
StatsTalk uses Microsoft Edge WebView2 for its native window. When WebView2 is missing
or cannot initialize, StatsTalk opens the same token-protected local interface in your
default browser. Install Microsoft Edge WebView2 Runtime to restore the native window.

Removal
-------
Close StatsTalk, then delete this folder. Delete Data as well to remove configuration,
the encrypted key, restore metadata, temporary workspaces, and crash-report state.
