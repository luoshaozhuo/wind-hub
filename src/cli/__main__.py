"""``python -m cli`` 入口。

必须经由这里（或 cli/main.py 的 __main__ 守卫）间接调用 main()，使运行期只有
一个 cli.main 模块实例；否则 runpy 会把 cli/main.py 再作为 __main__ 执行一遍，
子模块惰性导入的 ``cli.main`` 全局配置/运行时会与 __main__ 实例分裂。
"""

from cli.main import main

if __name__ == "__main__":
    main()
