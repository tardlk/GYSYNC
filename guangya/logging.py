"""运行日志过滤已配置的私有凭据，包括第三方异常文本。"""

import logging
from collections import deque

from guangya.config import SECRET_FIELDS


class SecretFormatter(logging.Formatter):
    """格式化日志后脱敏，覆盖 traceback 和轮换前的最近凭据。"""

    def __init__(self, config):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.config = config
        self.known = deque(maxlen=64)

    def format(self, record):
        values = self.config.snapshot().model_dump()
        for key in SECRET_FIELDS:
            value = values.get(key)
            if value and value not in self.known:
                self.known.append(value)
        rendered = super().format(record)
        for value in sorted(self.known, key=len, reverse=True):
            rendered = rendered.replace(value, "[已隐藏]")
        return rendered
