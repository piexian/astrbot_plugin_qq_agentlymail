from astrbot.api.star import Context, Star


class QQAgentlyMailPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """保留插件初始化入口。"""
        pass

    async def terminate(self):
        """保留插件销毁入口。"""
        pass
