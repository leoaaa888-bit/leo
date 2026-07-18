Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "E:\Users\Administrator\Desktop\web-scrcpy\web-scrcpy-main"
sh.Run "pythonw ""start_multi.py"" --hidden", 0, False