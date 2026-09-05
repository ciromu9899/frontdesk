FRONTDESK 1.5.0 - WINDOWS PORTABLE EDITION
==========================================

1. Extract the entire ZIP. Do not run FrontDesk.exe from inside the ZIP.
2. Double-click FrontDesk.exe.
3. Choose the market, chat language, and Local AI mode.
4. On first use, approve the 5.03 GB model download.
5. The customer chat and shared inbox open in your browser.

Python and Ollama do not need to be installed.

The model is downloaded from the pinned Qwen official repository and is not
installed unless its SHA-256 matches. App data is stored in:
%LOCALAPPDATA%\ShellieSoftwareTools\FrontDesk

To test the interface without downloading a model:
FrontDesk.exe --provider echo

Local URLs:
Customer chat  http://127.0.0.1:8766/
Shared inbox   http://127.0.0.1:8765/login

Keep the administrator token private. Close the FrontDesk console with Ctrl+C.
For internet access, place FrontDesk behind an HTTPS reverse proxy and follow
the connector-specific setup documentation. Do not expose the local ports
directly to the internet.

Seller: ShellieSoftwareTools
Website: https://gipan-bite.tech/
