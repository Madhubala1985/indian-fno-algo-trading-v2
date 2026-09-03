#!/usr/bin/env bash
# quick check: is MT5 algo trading enabled?
cd /mnt/c/Users/madhu/gold_shadow && /mnt/c/Windows/System32/cmd.exe /c \
  "cd /d C:\Users\madhu\gold_shadow && C:\Users\madhu\AppData\Local\Programs\Python\Python312\python.exe algocheck.py" 2>/dev/null | tr -d '\r'
