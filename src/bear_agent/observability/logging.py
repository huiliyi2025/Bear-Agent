"""Log redaction for the harness logger.

Command output is never logged directly, but provider/tool exceptions can carry
credential fragments. A filter on the ``bear_agent`` logger replaces registered
secret values and common credential shapes before handlers format the record.
"""

from __future__ import annotations

import logging

from bear_agent.secrets.redaction import SecretRedactor


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__(name="bear_agent")
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(str(record.msg))
        if record.args:
            record.args = self.redactor.redact_obj(record.args)
        return True


def install_redacting_filter(redactor: SecretRedactor) -> None:
    # Filters are not applied to records propagated from child loggers, so
    # install on every existing ``bear_agent.*`` logger, not just the parent.
    for name, logger in logging.Logger.manager.loggerDict.items():
        if not (name == "bear_agent" or name.startswith("bear_agent.")):
            continue
        if not isinstance(logger, logging.Logger):
            continue
        for item in logger.filters:
            if isinstance(item, RedactingFilter):
                item.redactor = redactor
                break
        else:
            logger.addFilter(RedactingFilter(redactor))
