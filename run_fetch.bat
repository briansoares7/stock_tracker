@echo off
REM run_fetch.bat — runs fetch_deals.py once. Point Task Scheduler at this file.
REM Edit the path below to wherever you put fetch_deals.py and deals.json.

cd /d "C:\Users\brian\Downloads\files"
python fetch_deals.py --out deals.json --include-bse >> fetch_log.txt 2>&1
