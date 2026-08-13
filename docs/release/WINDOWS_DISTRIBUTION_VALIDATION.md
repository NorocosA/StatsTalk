# Windows Distribution Validation

Complete this checklist for both a clean Windows 10 x64 environment and a clean
Windows 11 x64 environment before approving the 0.9 beta release.

## Build Evidence

- Release version:
- Release commit:
- Installer SHA-256:
- Portable ZIP SHA-256:
- Validator:
- Validation date:

## Installer

- [ ] Installation completes for a standard user without elevation.
- [ ] Start menu and uninstall entries are present.
- [ ] Optional desktop shortcut behaves as selected.
- [ ] First run opens the native window when WebView2 is available.
- [ ] Missing WebView2 shows guidance and opens the token-protected browser fallback.
- [ ] `.sav`, `.csv`, and `.xlsx` import behavior is unchanged.
- [ ] A no-key local analysis and Word/JSON export complete.
- [ ] Restart restores only an explicitly opted-in encrypted dataset reference.
- [ ] Uninstall with data retention selected leaves `%APPDATA%\StatsTalk` intact.
- [ ] Uninstall with data removal selected removes `%APPDATA%\StatsTalk`.
- [ ] Neither uninstall option deletes original datasets or exported `.stkb` backups.

## Portable ZIP

- [ ] The ZIP starts without installation or elevation.
- [ ] Configuration and encrypted state are created only in the adjacent `Data` folder.
- [ ] No portable state is written to `%APPDATA%\StatsTalk`.
- [ ] Moving the package explains that DPAPI data is account-bound and requires `.stkb` backup.
- [ ] Deleting the portable directory removes its application state without touching source data.

## Results

- Windows version and build:
- WebView2 version or missing-runtime result:
- Installer result:
- Portable result:
- Uninstall result:
- Known deviations:
- Reviewer signature:
- Decision: PENDING
