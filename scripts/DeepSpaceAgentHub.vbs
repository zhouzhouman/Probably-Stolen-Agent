' 开机自动启动 DeepSpace Agent Hub —— 游戏内「▶ 启动 Agent」按钮依赖它
' 取消自启：删掉本文件，以及启动文件夹里的同名副本
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\WorkBuddyData\深空当铺aiagent"
sh.Run """C:\Users\qwert\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"" ""scripts\agent_hub.py""", 0, False
