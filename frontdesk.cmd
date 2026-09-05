@echo off
rem Frontdesk launcher. Add this directory to PATH to run `frontdesk` from anywhere.
rem ASCII only: .cmd files are read in the OEM codepage, so non-ASCII breaks parsing.
python "%~dp0chat.py" %*
