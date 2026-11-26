@echo off
REM 启动 agent 守护进程（无窗口常驻）。游戏内「▶ 启动 Agent」按钮依赖它。
REM 用 pythonw 而不是 python：没有黑窗口，就不会被误关掉
REM （实测过一次：关掉那个窗口 = 守护直接死，面板就显示"启动器没在运行"）。
REM 想停守护：任务管理器结束 pythonw.exe。
cd /d "%~dp0.."
start "" /min "C:/Users/qwert/.workbuddy/binaries/python/envs/default/Scripts/pythonw.exe" scripts\agent_hub.py
exit /b 0
