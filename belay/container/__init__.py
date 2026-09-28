"""上传到任务容器里执行的脚本，例如检查运行器 runner.py。

这里的文件在容器内以独立脚本运行：只能用标准库，并兼容 Python 3.6；不能 import belay 的其他模块。
宿主机侧通过读取文件内容上传（Env.write_text），不 import 本目录。
"""
