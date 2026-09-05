# Third-party notices for the Windows portable edition

FrontDesk itself is licensed under Apache-2.0. The portable Windows build also
uses the following separately licensed components.

## llama.cpp b10516

- Project: https://github.com/ggml-org/llama.cpp
- Release: https://github.com/ggml-org/llama.cpp/releases/tag/b10516
- Windows CPU asset SHA-256:
  `fbbbc55e0eb2e1b07f9dcb9488616c98ed47d9003b90e15e7c8c7812c4307cd3`
- License: MIT

Copyright (c) 2023-2026 The ggml authors. Permission is granted, free of
charge, to any person obtaining a copy of this software and associated
documentation files to deal in the Software without restriction, subject to
the conditions in the project's LICENSE file.

## Qwen3-8B-GGUF

- Project: https://huggingface.co/Qwen/Qwen3-8B-GGUF
- Revision: `1d54a16a18cba0d8fbad4a16db801decc729e099`
- File: `Qwen3-8B-Q4_K_M.gguf`
- SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`
- License: Apache-2.0

The model is not embedded in the Windows ZIP. It is downloaded only after the
operator consents, and FrontDesk verifies the pinned SHA-256 before use.

## CPython and Python libraries

The portable build contains a CPython runtime under the Python Software
Foundation License and the application dependencies listed in `SBOM.cdx.json`.
PyInstaller is used as a build tool under GPL-2.0-or-later with its exception
permitting distribution of the resulting executable under the application's
license.
